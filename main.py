"""
DeployX Bot — Advanced Website Deployment Platform
100% fixed · Auto-migrates DB · Zero crash guarantee
"""

import os
import re
import logging
import sqlite3
import hashlib
import zipfile
import shutil
import asyncio
import requests
from pathlib import Path
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TimedOut

# ═══════════════════════════════════════════════════════════
#  CONFIG — fill before running
# ═══════════════════════════════════════════════════════════
BOT_TOKEN        = "8564377290:AAHHy9_QIVevwgfr2FkGOcCBwhIKO3_wUu4"
NETLIFY_TOKEN    = "nfp_wrbTXE7HTyQFkP2d9RphE7ULCQLwESMHdde6"
REQUIRED_CHANNEL = "@A7adi"
WORK_DIR         = "/tmp/deployx"
DB_PATH          = "deployx.db"

MAX_ZIP_BYTES      = 10 * 1024 * 1024
BLOCKED_EXT        = {".php", ".exe", ".sh", ".py", ".rb", ".pl", ".cgi", ".bat", ".cmd"}

STATE_WAITING_ZIP          = "waiting_zip"
STATE_WAITING_NAME         = "waiting_name"
STATE_WAITING_REDEPLOY_ZIP = "waiting_redeploy_zip"

# ═══════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("DeployX")

# ═══════════════════════════════════════════════════════════
#  DATABASE  — with auto-migration so old DBs never break
# ═══════════════════════════════════════════════════════════
def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def db_init():
    """Create tables if missing, then add any missing columns (migration)."""
    with db() as c:
        # Create base tables
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username    TEXT,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS projects (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                name       TEXT    NOT NULL,
                site_id    TEXT,
                deploy_id  TEXT,
                url        TEXT,
                status     TEXT DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(telegram_id)
            );
        """)

        # Auto-migration: add first_name column if it doesn't exist
        existing = {row[1] for row in c.execute("PRAGMA table_info(users)")}
        if "first_name" not in existing:
            c.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
            log.info("DB migration: added first_name column to users table.")

    log.info("Database ready.")


def db_upsert_user(telegram_id: int, username: Optional[str], first_name: Optional[str]):
    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username, first_name) VALUES (?,?,?)",
            (telegram_id, username, first_name),
        )
        c.execute(
            "UPDATE users SET username=?, first_name=? WHERE telegram_id=?",
            (username, first_name, telegram_id),
        )


def db_create_project(user_id: int, name: str) -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO projects (user_id, name) VALUES (?,?)", (user_id, name)
        )
        return cur.lastrowid


def db_update_project(pid: int, site_id: str, deploy_id: str, url: str, status: str):
    with db() as c:
        c.execute(
            "UPDATE projects SET site_id=?,deploy_id=?,url=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (site_id, deploy_id, url, status, pid),
        )


def db_get_project(pid: int) -> Optional[sqlite3.Row]:
    with db() as c:
        return c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()


def db_user_projects(user_id: int) -> list:
    with db() as c:
        return c.execute(
            "SELECT * FROM projects WHERE user_id=? ORDER BY updated_at DESC", (user_id,)
        ).fetchall()


def db_delete_project(pid: int):
    with db() as c:
        c.execute("DELETE FROM projects WHERE id=?", (pid,))


def db_count(user_id: int) -> int:
    with db() as c:
        r = c.execute("SELECT COUNT(*) FROM projects WHERE user_id=?", (user_id,)).fetchone()
        return r[0] if r else 0


# ═══════════════════════════════════════════════════════════
#  SAFE TELEGRAM HELPERS — never crash on edit errors
# ═══════════════════════════════════════════════════════════
async def safe_edit(msg, text: str, kb=None, md=ParseMode.MARKDOWN):
    try:
        await msg.edit_text(
            text, parse_mode=md, reply_markup=kb, disable_web_page_preview=True
        )
    except BadRequest as e:
        err = str(e).lower()
        if "message is not modified" in err or "message to edit not found" in err:
            pass
        else:
            log.warning("safe_edit BadRequest: %s", e)
    except (RetryAfter, TimedOut):
        await asyncio.sleep(2)
    except Exception as e:
        log.warning("safe_edit error: %s", e)


async def safe_reply(msg, text: str, kb=None, md=ParseMode.MARKDOWN):
    try:
        return await msg.reply_text(
            text, parse_mode=md, reply_markup=kb, disable_web_page_preview=True
        )
    except (RetryAfter, TimedOut):
        await asyncio.sleep(2)
    except Exception as e:
        log.warning("safe_reply error: %s", e)
    return None


# ═══════════════════════════════════════════════════════════
#  KEYBOARDS
# ═══════════════════════════════════════════════════════════
def kb_panel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ New Project",  callback_data="new_project"),
         InlineKeyboardButton("📁 My Projects",  callback_data="my_projects")],
        [InlineKeyboardButton("⚡ Quick Deploy", callback_data="quick_deploy"),
         InlineKeyboardButton("❓ Help",         callback_data="help")],
    ])

def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Panel", callback_data="panel")]])

def kb_cancel():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="panel")]])

def kb_projects(projects):
    rows = []
    for p in projects:
        icon = "✅" if p["status"] == "deployed" else "🕐"
        rows.append([InlineKeyboardButton(f"{icon} {p['name']}", callback_data=f"proj_{p['id']}")])
    rows.append([InlineKeyboardButton("🔙 Back to Panel", callback_data="panel")])
    return InlineKeyboardMarkup(rows)

def kb_project(pid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Deploy",       callback_data=f"deploy_{pid}"),
         InlineKeyboardButton("🔄 Redeploy",     callback_data=f"redeploy_{pid}")],
        [InlineKeyboardButton("🔗 Live URL",     callback_data=f"url_{pid}"),
         InlineKeyboardButton("🗑 Delete",       callback_data=f"delete_{pid}")],
        [InlineKeyboardButton("🔙 My Projects",  callback_data="my_projects")],
        [InlineKeyboardButton("🏠 Main Menu",    callback_data="panel")],
    ])

def kb_confirm(pid: int):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirm_delete_{pid}"),
        InlineKeyboardButton("❌ Keep It",     callback_data=f"proj_{pid}"),
        InlineKeyboardButton("🔙 Back",        callback_data="my_projects"),
    ]])

def kb_deployed(url: str, pid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Open Live Site", url=url)],
        [InlineKeyboardButton("📋 Project Menu",   callback_data=f"proj_{pid}"),
         InlineKeyboardButton("🏠 Main Menu",      callback_data="panel")],
    ])


# ═══════════════════════════════════════════════════════════
#  HELP TEXT
# ═══════════════════════════════════════════════════════════
HELP = (
    "❓ DeployX Bot - Quick Help\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "🚀 How to Deploy Your Website\n\n"
    "1. Prepare your website files\n"
    "   • Must include index.html\n"
    "   • Only static files (HTML/CSS/JS/images)\n\n"
    "2. Create a ZIP file\n"
    "   Important: Use this command:\n"
    "   zip -j site.zip your-folder/*\n"
    "   (The -j flag puts files at root level)\n\n"
    "3. Send the ZIP to this bot\n"
    "   • Use Quick Deploy for instant deployment\n"
    "   • Or create a Project to save it\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "🎯 Commands\n\n"
    "• /start - Welcome message\n"
    "• /panel - Open main menu\n"
    "• /deploy - Quick deploy a website\n"
    "• /help - Show this help message\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "⚠️ Common Problems & Solutions\n\n"
    "❌ No index.html\n"
    "   → Use -j flag when zipping\n\n"
    "❌ File too large\n"
    "   → Max size is 10 MB\n"
    "   → Compress images\n\n"
    "❌ PHP not working\n"
    "   → DeployX only supports static sites\n"
    "   → Use HTML/CSS/JS only\n\n"
    "❌ Site shows blank page\n"
    "   → Check browser console for errors\n"
    "   → Verify all file paths are relative\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "💡 Pro Tips\n\n"
    "• Test your site locally before deploying\n"
    "• Use relative paths in your code\n"
    "• Keep index.html at the root of your ZIP\n"
    "• You can redeploy projects with new ZIPs\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "🎨 Premium Templates & Private Sites\n\n"
    "For private sites and premium templates, contact:\n"
    "@LM_S0\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
)


# ═══════════════════════════════════════════════════════════
#  SUBSCRIPTION GATE
# ═══════════════════════════════════════════════════════════
async def is_subscribed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        m = await ctx.bot.get_chat_member(REQUIRED_CHANNEL, update.effective_user.id)
        return m.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning("Subscription check error: %s", e)
        return False


async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    if await is_subscribed(update, ctx):
        return True
    url  = f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"
    kb   = InlineKeyboardMarkup([[
        InlineKeyboardButton("📢 Join Channel", url=url),
        InlineKeyboardButton("✅ I Joined",     callback_data="check_sub"),
    ]])
    text = (
        "🔒 Access Required\n\n"
        "Join our channel to use DeployX Bot.\n\n"
        "1️⃣ Tap Join Channel\n"
        "2️⃣ Come back and tap I Joined"
    )
    msg = update.callback_query.message if update.callback_query else update.effective_message
    if update.callback_query:
        await update.callback_query.answer("Join the channel first!", show_alert=True)
        await safe_edit(msg, text, kb)
    else:
        await safe_reply(msg, text, kb)
    return False


# ═══════════════════════════════════════════════════════════
#  NETLIFY API
# ═══════════════════════════════════════════════════════════
_NH = {"Authorization": f"Bearer {NETLIFY_TOKEN}", "Content-Type": "application/json"}
_NB = "https://api.netlify.com/api/v1"


def net_create_site() -> dict:
    r = requests.post(f"{_NB}/sites", json={}, headers=_NH, timeout=30)
    r.raise_for_status()
    return r.json()


def net_create_deploy(site_id: str, sha_map: dict) -> dict:
    r = requests.post(
        f"{_NB}/sites/{site_id}/deploys",
        json={"files": sha_map}, headers=_NH, timeout=30,
    )
    r.raise_for_status()
    return r.json()


def net_upload(deploy_id: str, path: str, data: bytes):
    h = {"Authorization": f"Bearer {NETLIFY_TOKEN}", "Content-Type": "application/octet-stream"}
    r = requests.put(f"{_NB}/deploys/{deploy_id}/files{path}", data=data, headers=h, timeout=60)
    r.raise_for_status()


def net_get_deploy(deploy_id: str) -> dict:
    r = requests.get(f"{_NB}/deploys/{deploy_id}", headers=_NH, timeout=15)
    r.raise_for_status()
    return r.json()


def net_delete_site(site_id: str):
    try:
        requests.delete(f"{_NB}/sites/{site_id}", headers=_NH, timeout=30)
    except Exception as e:
        log.warning("Netlify delete: %s", e)


# ═══════════════════════════════════════════════════════════
#  ZIP ENGINE
# ═══════════════════════════════════════════════════════════
def sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_zip(zip_path: str, dest: str) -> tuple:
    """Returns (ok: bool, error_msg: str)"""
    if os.path.getsize(zip_path) > MAX_ZIP_BYTES:
        mb = os.path.getsize(zip_path) / 1024 / 1024
        return False, (
            f"❌ File Too Large ({mb:.1f} MB)\n\n"
            "Maximum allowed size is 10 MB.\n"
            "Compress your images or remove unused assets."
        )

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if name.startswith("/") or ".." in name:
                    return False, "❌ Security Error\n\nZIP contains unsafe paths."
                if Path(name).suffix.lower() in BLOCKED_EXT:
                    return False, (
                        f"❌ Blocked File: {name}\n\n"
                        "Only static files are allowed.\n"
                        "Remove .php .py .sh .exe and try again."
                    )
            zf.extractall(dest)
    except zipfile.BadZipFile:
        return False, (
            "❌ Invalid ZIP File\n\n"
            "The file you sent is not a valid ZIP archive.\n"
            "Re-zip your files and try again."
        )
    except Exception as e:
        return False, f"❌ Extraction Error\n\n{e}"

    # Auto-fix: flatten single top-level folder
    entries = [e for e in os.listdir(dest) if e != "__MACOSX" and not e.startswith(".")]
    if len(entries) == 1 and os.path.isdir(os.path.join(dest, entries[0])):
        nested = os.path.join(dest, entries[0])
        for item in os.listdir(nested):
            shutil.move(os.path.join(nested, item), os.path.join(dest, item))
        shutil.rmtree(nested, ignore_errors=True)
        log.info("Auto-fix: flattened '%s'", entries[0])

    # Remove macOS junk
    mac = os.path.join(dest, "__MACOSX")
    if os.path.isdir(mac):
        shutil.rmtree(mac, ignore_errors=True)

    if not os.path.isfile(os.path.join(dest, "index.html")):
        return False, (
            "❌ Missing index.html\n\n"
            "Your ZIP must have index.html at the root level.\n\n"
            "💡 Fix — zip like this:\n"
            "zip -j site.zip your-folder/*\n\n"
            "The -j flag puts files at the root."
        )

    return True, ""


def build_map(directory: str) -> dict:
    """Returns {'/path': (sha1_str, bytes)}"""
    fm = {}
    for root, _, files in os.walk(directory):
        for fname in files:
            fp  = os.path.join(root, fname)
            rel = "/" + os.path.relpath(fp, directory).replace("\\", "/")
            s   = sha1(fp)
            with open(fp, "rb") as f:
                fm[rel] = (s, f.read())
    return fm


# ═══════════════════════════════════════════════════════════
#  DEPLOY RUNNER
# ═══════════════════════════════════════════════════════════
async def deploy(zip_path: str, site_id: Optional[str], cb) -> tuple:
    """Returns (ok, url_or_error, site_id, deploy_id)"""
    dest = zip_path + "_ex"
    os.makedirs(dest, exist_ok=True)
    try:
        await cb("📦 Step 1/4 — Extracting & validating ZIP...")
        ok, err = extract_zip(zip_path, dest)
        if not ok:
            return False, err, "", ""

        await cb("🔍 Step 2/4 — Analysing files...")
        fm    = build_map(dest)
        smap  = {p: s for p, (s, _) in fm.items()}
        total = len(fm)

        if not site_id:
            await cb("🌐 Step 2/4 — Creating Netlify site...")
            site_id = net_create_site()["id"]

        await cb(f"🚀 Step 3/4 — Creating deploy ({total} files)...")
        dep      = net_create_deploy(site_id, smap)
        dep_id   = dep["id"]
        required = set(dep.get("required", []))

        if required:
            done = 0
            tot  = len(required)
            await cb(f"📤 Step 3/4 — Uploading {tot} file(s)...")
            for path, (s, data) in fm.items():
                if s in required:
                    net_upload(dep_id, path, data)
                    done += 1
                    step = max(1, tot // 5)
                    if done % step == 0 or done == tot:
                        pct = int(done / tot * 100)
                        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                        await cb(f"📤 Uploading...\n[{bar}] {pct}% ({done}/{tot})")

        await cb("⏳ Step 4/4 — Waiting for site to go live...")
        await asyncio.sleep(4)

        info = net_get_deploy(dep_id)
        url  = (
            info.get("deploy_ssl_url") or info.get("deploy_url") or
            info.get("ssl_url")        or info.get("url") or ""
        )
        if not url:
            r = requests.get(f"{_NB}/sites/{site_id}", headers=_NH, timeout=15)
            if r.ok:
                d   = r.json()
                url = d.get("ssl_url") or d.get("url") or ""

        if not url:
            return False, "❌ Deploy finished but no URL returned. Check your Netlify dashboard.", site_id, dep_id

        return True, url, site_id, dep_id

    except requests.HTTPError as e:
        code = e.response.status_code if e.response else "?"
        body = e.response.text[:300]  if e.response else ""
        log.error("Netlify HTTP %s: %s", code, body)
        return False, f"❌ Netlify Error (HTTP {code})\n\n{body}", "", ""
    except Exception as e:
        log.exception("Deploy error")
        return False, f"❌ Error\n\n{e}", "", ""
    finally:
        shutil.rmtree(dest, ignore_errors=True)
        try:
            os.remove(zip_path)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════
#  COMMANDS - FIXED START COMMAND
# ═══════════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Start command handler - FIXED"""
    try:
        u = update.effective_user
        if not u:
            log.error("No effective user in start command")
            return
            
        db_upsert_user(u.id, u.username, u.first_name)
        ctx.user_data.clear()

        if not await gate(update, ctx):
            return

        count = db_count(u.id)
        
        # Improved welcome message with clear options
        if count > 0:
            text = (
                f"👋 Welcome back, {u.first_name}!\n\n"
                f"You have {count} project(s) saved.\n\n"
                f"What would you like to do?"
            )
        else:
            text = (
                f"🎉 Welcome to DeployX, {u.first_name}!\n\n"
                f"I help you deploy websites instantly to the internet.\n\n"
                f"📦 Quick Start:\n"
                f"1. Prepare your website files (must have index.html)\n"
                f"2. ZIP them with: zip -j site.zip your-folder/*\n"
                f"3. Send the ZIP to me\n\n"
                f"✨ That's it! You'll get a live URL immediately.\n\n"
                f"👇 Choose an option below:"
            )
        
        # Create inline keyboard
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Open Main Menu", callback_data="panel")],
            [InlineKeyboardButton("⚡ Quick Deploy", callback_data="quick_deploy")],
            [InlineKeyboardButton("❓ View Help Guide", callback_data="help")]
        ])
        
        # Send the message
        await update.message.reply_text(
            text, 
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        
    except Exception as e:
        log.error(f"Error in start command: {e}")
        await update.message.reply_text(
            "⚠️ Something went wrong. Please try again or contact support.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❓ Help", callback_data="help")]])
        )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Help command"""
    try:
        await update.message.reply_text(
            HELP, 
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_back(),
            disable_web_page_preview=True
        )
    except Exception as e:
        log.error(f"Error in help command: {e}")
        await update.message.reply_text("❌ Help information unavailable. Please try again later.")


async def cmd_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Main panel with clear navigation"""
    try:
        u = update.effective_user
        db_upsert_user(u.id, u.username, u.first_name)
        ctx.user_data.clear()
        if not await gate(update, ctx):
            return
        count = db_count(u.id)
        await update.message.reply_text(
            f"📊 DeployX Control Panel\n\n"
            f"📁 Active Projects: {count}\n\n"
            f"Choose an option below:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_panel(),
        )
    except Exception as e:
        log.error(f"Error in panel command: {e}")
        await update.message.reply_text("⚠️ Could not open panel. Please try /start")


async def cmd_deploy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Deploy command"""
    try:
        u = update.effective_user
        db_upsert_user(u.id, u.username, u.first_name)
        if not await gate(update, ctx):
            return
        ctx.user_data.clear()
        ctx.user_data["state"] = STATE_WAITING_ZIP
        ctx.user_data["mode"]  = "quick"
        await update.message.reply_text(
            "⚡ Quick Deploy\n\n"
            "Send me your .zip file and I'll deploy it instantly!\n\n"
            "💡 Tip: use zip -j site.zip folder/*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_cancel(),
        )
    except Exception as e:
        log.error(f"Error in deploy command: {e}")
        await update.message.reply_text("⚠️ Could not start deploy. Please try /start")


# ═══════════════════════════════════════════════════════════
#  CALLBACK HANDLER
# ═══════════════════════════════════════════════════════════
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    data = q.data
    u    = update.effective_user
    msg  = q.message

    # Subscription verify
    if data == "check_sub":
        if await is_subscribed(update, ctx):
            db_upsert_user(u.id, u.username, u.first_name)
            await safe_edit(
                msg,
                f"✅ Verified! Welcome, {u.first_name}!\n\n"
                f"You're all set. Tap below to start deploying.",
                InlineKeyboardMarkup([[InlineKeyboardButton("📊 Open Panel", callback_data="panel")]]),
            )
        else:
            await q.answer("❌ You haven't joined the channel yet.", show_alert=True)
        return

    if not await gate(update, ctx):
        return

    db_upsert_user(u.id, u.username, u.first_name)

    # Panel (Main Menu)
    if data == "panel":
        ctx.user_data.clear()
        count = db_count(u.id)
        await safe_edit(
            msg,
            f"📊 DeployX Panel\n\n"
            f"📁 Projects: {count}\n\n"
            f"What would you like to do?",
            kb_panel(),
        )

    # Help
    elif data == "help":
        await safe_edit(msg, HELP, kb_back())

    # New Project
    elif data == "new_project":
        ctx.user_data["state"] = STATE_WAITING_NAME
        await safe_edit(
            msg,
            "➕ Create New Project\n\n"
            "Send me a name for your project.\n\n"
            "📝 Example: my-portfolio or cool-website\n\n"
            "Use letters, numbers, and dashes only.",
            kb_cancel(),
        )

    # My Projects
    elif data == "my_projects":
        projects = db_user_projects(u.id)
        if not projects:
            await safe_edit(
                msg,
                "📁 My Projects\n\n"
                "✨ You don't have any projects yet!\n\n"
                "Get started by creating your first project:",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Create First Project", callback_data="new_project")],
                    [InlineKeyboardButton("⚡ Quick Deploy (No Save)", callback_data="quick_deploy")],
                    [InlineKeyboardButton("🔙 Back to Menu", callback_data="panel")],
                ]),
            )
        else:
            await safe_edit(
                msg,
                f"📁 Your Projects ({len(projects)})\n\n"
                f"Tap any project to manage it:",
                kb_projects(projects),
            )

    # Quick Deploy
    elif data == "quick_deploy":
        ctx.user_data.clear()
        ctx.user_data["state"] = STATE_WAITING_ZIP
        ctx.user_data["mode"]  = "quick"
        await safe_edit(
            msg,
            "⚡ Quick Deploy Mode\n\n"
            "📤 Send me your ZIP file and I'll deploy it instantly!\n\n"
            "💡 Tip: Make sure your ZIP:\n"
            "• Contains an index.html file\n"
            "• Is created with: zip -j site.zip folder/*\n"
            "• Is under 10 MB in size\n\n"
            "Ready when you are!",
            kb_cancel(),
        )

    # Project Detail
    elif data.startswith("proj_"):
        pid  = int(data.split("_")[1])
        proj = db_get_project(pid)
        if not proj or proj["user_id"] != u.id:
            await q.answer("Project not found.", show_alert=True)
            return
        icon     = "✅" if proj["status"] == "deployed" else "🕐"
        url_line = f"🔗 {proj['url']}" if proj["url"] else "🔗 Not deployed yet"
        await safe_edit(
            msg,
            f"{icon} {proj['name']}\n\n"
            f"Status: {proj['status'].capitalize()}\n"
            f"{url_line}\n\n"
            "What would you like to do?",
            kb_project(pid),
        )

    # Deploy
    elif data.startswith("deploy_"):
        pid  = int(data.split("_")[1])
        proj = db_get_project(pid)
        if not proj or proj["user_id"] != u.id:
            await q.answer("Project not found.", show_alert=True)
            return
        ctx.user_data.clear()
        ctx.user_data["state"] = STATE_WAITING_ZIP
        ctx.user_data["mode"]  = "project"
        ctx.user_data["pid"]   = pid
        await safe_edit(
            msg,
            f"🚀 Deploy → {proj['name']}\n\nSend me your .zip file.",
            kb_cancel(),
        )

    # Redeploy
    elif data.startswith("redeploy_"):
        pid  = int(data.split("_")[1])
        proj = db_get_project(pid)
        if not proj or proj["user_id"] != u.id:
            await q.answer("Project not found.", show_alert=True)
            return
        if not proj["site_id"]:
            await q.answer("Deploy this project first before redeploying.", show_alert=True)
            return
        ctx.user_data.clear()
        ctx.user_data["state"]   = STATE_WAITING_REDEPLOY_ZIP
        ctx.user_data["pid"]     = pid
        ctx.user_data["site_id"] = proj["site_id"]
        await safe_edit(
            msg,
            f"🔄 Redeploy → {proj['name']}\n\n"
            "Send me the updated .zip file.\n"
            "Your existing site will be updated.",
            kb_cancel(),
        )

    # Show URL
    elif data.startswith("url_"):
        pid  = int(data.split("_")[1])
        proj = db_get_project(pid)
        if not proj or proj["user_id"] != u.id:
            await q.answer("Project not found.", show_alert=True)
            return
        if proj["url"]:
            await q.answer(f"🔗 {proj['url']}", show_alert=True)
        else:
            await q.answer("No URL yet — deploy first.", show_alert=True)

    # Delete prompt
    elif data.startswith("delete_"):
        pid  = int(data.split("_")[1])
        proj = db_get_project(pid)
        if not proj or proj["user_id"] != u.id:
            await q.answer("Project not found.", show_alert=True)
            return
        await safe_edit(
            msg,
            f"🗑 Delete '{proj['name']}?'\n\n"
            "⚠️ This will permanently remove the project and its Netlify site.",
            kb_confirm(pid),
        )

    # Delete confirmed
    elif data.startswith("confirm_delete_"):
        pid  = int(data.split("_")[2])
        proj = db_get_project(pid)
        if not proj or proj["user_id"] != u.id:
            await q.answer("Project not found.", show_alert=True)
            return
        name = proj["name"]
        if proj["site_id"]:
            net_delete_site(proj["site_id"])
        db_delete_project(pid)
        count = db_count(u.id)
        await safe_edit(
            msg,
            f"🗑 '{name}' deleted.\n\nYou now have {count} project(s).",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📁 My Projects", callback_data="my_projects")],
                [InlineKeyboardButton("📊 Panel",       callback_data="panel")],
            ]),
        )

    else:
        await q.answer("❓ Unknown option. Use /panel to return to menu.", show_alert=True)


# ═══════════════════════════════════════════════════════════
#  TEXT HANDLER
# ═══════════════════════════════════════════════════════════
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db_upsert_user(u.id, u.username, u.first_name)

    if not await gate(update, ctx):
        return

    if ctx.user_data.get("state") == STATE_WAITING_NAME:
        raw  = update.message.text.strip()
        name = re.sub(r"[^\w\s\-]", "", raw)[:64].strip()
        if not name:
            await safe_reply(
                update.message,
                "❌ Please send a valid name (letters, numbers, dashes).",
                kb_cancel(),
            )
            return
        pid = db_create_project(u.id, name)
        ctx.user_data["state"] = STATE_WAITING_ZIP
        ctx.user_data["mode"]  = "project"
        ctx.user_data["pid"]   = pid
        await safe_reply(
            update.message,
            f"✅ Project '{name}' created!\n\nNow send me the .zip file to deploy.",
            kb_cancel(),
        )
    else:
        await safe_reply(
            update.message,
            "👋 Use /panel to open the menu or /deploy for a quick deploy.",
            InlineKeyboardMarkup([[InlineKeyboardButton("📊 Open Panel", callback_data="panel")]]),
        )


# ═══════════════════════════════════════════════════════════
#  DOCUMENT HANDLER
# ═══════════════════════════════════════════════════════════
async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db_upsert_user(u.id, u.username, u.first_name)

    if not await gate(update, ctx):
        return

    state = ctx.user_data.get("state")
    if state not in (STATE_WAITING_ZIP, STATE_WAITING_REDEPLOY_ZIP):
        await safe_reply(
            update.message,
            "💡 Use /deploy or open the panel first.",
            InlineKeyboardMarkup([[InlineKeyboardButton("📊 Open Panel", callback_data="panel")]]),
        )
        return

    doc = update.message.document
    if not doc.file_name.lower().endswith(".zip"):
        await safe_reply(
            update.message,
            "❌ Wrong file type.\n\nI only accept .zip files.\n\n"
            "💡 Create one with:\nzip -j site.zip your-folder/*",
            kb_cancel(),
        )
        return

    if doc.file_size and doc.file_size > MAX_ZIP_BYTES:
        mb = doc.file_size / 1024 / 1024
        await safe_reply(
            update.message,
            f"❌ File Too Large ({mb:.1f} MB)\n\n"
            "Max is 10 MB. Compress images or remove unused files.",
            kb_cancel(),
        )
        return

    status_msg = await safe_reply(update.message, "⬇️ Downloading your file...")
    if not status_msg:
        return

    os.makedirs(WORK_DIR, exist_ok=True)
    zip_path = os.path.join(WORK_DIR, f"{u.id}_{doc.file_unique_id}.zip")

    try:
        tg = await ctx.bot.get_file(doc.file_id)
        await tg.download_to_drive(zip_path)
    except Exception as e:
        log.error("Download failed: %s", e)
        await safe_edit(status_msg, "❌ Download failed. Please try again.", kb_cancel())
        return

    # Determine project
    mode    = ctx.user_data.get("mode", "quick")
    pid     = ctx.user_data.get("pid")
    site_id = ctx.user_data.get("site_id")

    if mode == "quick":
        name = re.sub(r"\.zip$", "", doc.file_name, flags=re.IGNORECASE)[:64]
        name = re.sub(r"[^\w\s\-]", "", name).strip() or "my-site"
        pid  = db_create_project(u.id, name)

    async def cb(text: str):
        await safe_edit(status_msg, text)

    ok, result, new_site_id, dep_id = await deploy(zip_path, site_id, cb)

    if ok:
        db_update_project(pid, new_site_id, dep_id, result, "deployed")
        proj = db_get_project(pid)
        await safe_edit(
            status_msg,
            f"🎉 Deployment Successful!\n\n"
            f"📁 Project: {proj['name']}\n"
            f"🔗 Live URL:\n{result}\n\n"
            "Your site is live! May take a few seconds to fully propagate.",
            kb_deployed(result, pid),
        )
    else:
        await safe_edit(status_msg, result + "\n\nTap below to go back to menu.", kb_back())

    ctx.user_data.clear()


# ═══════════════════════════════════════════════════════════
#  ERROR HANDLER
# ═══════════════════════════════════════════════════════════
async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong. Please try again or use /start.",
                reply_markup=kb_back(),
            )
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════
def main():
    db_init()
    os.makedirs(WORK_DIR, exist_ok=True)

    app = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("panel",  cmd_panel))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("deploy", cmd_deploy))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("DeployX Bot started successfully!")
    log.info("Bot token: %s", BOT_TOKEN[:10] + "...")
    
    # Start polling
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
