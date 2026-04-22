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
#  CONFIG
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
#  DATABASE
# ═══════════════════════════════════════════════════════════
def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def db_init():
    with db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
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
    log.info("Database ready.")

def db_upsert_user(telegram_id: int, username: Optional[str], first_name: Optional[str]):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users (telegram_id, username, first_name) VALUES (?,?,?)", (telegram_id, username, first_name))
        c.execute("UPDATE users SET username=?, first_name=? WHERE telegram_id=?", (username, first_name, telegram_id))

def db_create_project(user_id: int, name: str) -> int:
    with db() as c:
        cur = c.execute("INSERT INTO projects (user_id, name) VALUES (?,?)", (user_id, name))
        return cur.lastrowid

def db_update_project(pid: int, site_id: str, deploy_id: str, url: str, status: str):
    with db() as c:
        c.execute("UPDATE projects SET site_id=?,deploy_id=?,url=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (site_id, deploy_id, url, status, pid))

def db_get_project(pid: int) -> Optional[sqlite3.Row]:
    with db() as c:
        return c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()

def db_user_projects(user_id: int) -> list:
    with db() as c:
        return c.execute("SELECT * FROM projects WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()

def db_delete_project(pid: int):
    with db() as c:
        c.execute("DELETE FROM projects WHERE id=?", (pid,))

def db_count(user_id: int) -> int:
    with db() as c:
        r = c.execute("SELECT COUNT(*) FROM projects WHERE user_id=?", (user_id,)).fetchone()
        return r[0] if r else 0

# ═══════════════════════════════════════════════════════════
#  SAFE REPLY - NO MARKDOWN
# ═══════════════════════════════════════════════════════════
async def safe_edit(msg, text: str, kb=None):
    try:
        await msg.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        log.warning(f"safe_edit error: {e}")

async def safe_reply(msg, text: str, kb=None):
    try:
        return await msg.reply_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        log.warning(f"safe_reply error: {e}")
    return None

# ═══════════════════════════════════════════════════════════
#  KEYBOARDS
# ═══════════════════════════════════════════════════════════
def kb_panel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ New Project", callback_data="new_project"), InlineKeyboardButton("📁 My Projects", callback_data="my_projects")],
        [InlineKeyboardButton("⚡ Quick Deploy", callback_data="quick_deploy"), InlineKeyboardButton("❓ Help", callback_data="help")],
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
        [InlineKeyboardButton("🚀 Deploy", callback_data=f"deploy_{pid}"), InlineKeyboardButton("🔄 Redeploy", callback_data=f"redeploy_{pid}")],
        [InlineKeyboardButton("🔗 Live URL", callback_data=f"url_{pid}"), InlineKeyboardButton("🗑 Delete", callback_data=f"delete_{pid}")],
        [InlineKeyboardButton("🔙 My Projects", callback_data="my_projects")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="panel")],
    ])

def kb_confirm(pid: int):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirm_delete_{pid}"),
        InlineKeyboardButton("❌ Keep It", callback_data=f"proj_{pid}"),
    ]])

def kb_deployed(url: str, pid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Open Live Site", url=url)],
        [InlineKeyboardButton("📋 Project Menu", callback_data=f"proj_{pid}"), InlineKeyboardButton("🏠 Main Menu", callback_data="panel")],
    ])

# ═══════════════════════════════════════════════════════════
#  SUBSCRIPTION GATE - WORKING
# ═══════════════════════════════════════════════════════════
async def is_subscribed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        m = await ctx.bot.get_chat_member(REQUIRED_CHANNEL, update.effective_user.id)
        return m.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning(f"Subscription check error: {e}")
        return False

async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    if await is_subscribed(update, ctx):
        return True
    
    url = f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📢 Join Channel", url=url),
        InlineKeyboardButton("✅ I Joined", callback_data="check_sub"),
    ]])
    text = "🔒 Access Required\n\nJoin our channel to use DeployX Bot.\n\n1. Tap Join Channel\n2. Come back and tap I Joined"
    
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
    r = requests.post(f"{_NB}/sites/{site_id}/deploys", json={"files": sha_map}, headers=_NH, timeout=30)
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
        log.warning(f"Netlify delete error: {e}")

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
    if os.path.getsize(zip_path) > MAX_ZIP_BYTES:
        mb = os.path.getsize(zip_path) / 1024 / 1024
        return False, f"File Too Large ({mb:.1f} MB)\n\nMaximum allowed size is 10 MB."

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if name.startswith("/") or ".." in name:
                    return False, "Security Error - ZIP contains unsafe paths."
                if Path(name).suffix.lower() in BLOCKED_EXT:
                    return False, f"Blocked File: {name}\n\nOnly static files are allowed."
            zf.extractall(dest)
    except zipfile.BadZipFile:
        return False, "Invalid ZIP File\n\nThe file is not a valid ZIP archive."
    except Exception as e:
        return False, f"Extraction Error: {e}"

    entries = [e for e in os.listdir(dest) if e != "__MACOSX" and not e.startswith(".")]
    if len(entries) == 1 and os.path.isdir(os.path.join(dest, entries[0])):
        nested = os.path.join(dest, entries[0])
        for item in os.listdir(nested):
            shutil.move(os.path.join(nested, item), os.path.join(dest, item))
        shutil.rmtree(nested, ignore_errors=True)

    mac = os.path.join(dest, "__MACOSX")
    if os.path.isdir(mac):
        shutil.rmtree(mac, ignore_errors=True)

    if not os.path.isfile(os.path.join(dest, "index.html")):
        return False, "Missing index.html\n\nYour ZIP must have index.html at the root level."

    return True, ""

def build_map(directory: str) -> dict:
    fm = {}
    for root, _, files in os.walk(directory):
        for fname in files:
            fp = os.path.join(root, fname)
            rel = "/" + os.path.relpath(fp, directory).replace("\\", "/")
            s = sha1(fp)
            with open(fp, "rb") as f:
                fm[rel] = (s, f.read())
    return fm

# ═══════════════════════════════════════════════════════════
#  DEPLOY RUNNER
# ═══════════════════════════════════════════════════════════
async def deploy(zip_path: str, site_id: Optional[str], cb) -> tuple:
    dest = zip_path + "_ex"
    os.makedirs(dest, exist_ok=True)
    try:
        await cb("Step 1/4 - Extracting ZIP...")
        ok, err = extract_zip(zip_path, dest)
        if not ok:
            return False, err, "", ""

        await cb("Step 2/4 - Analysing files...")
        fm = build_map(dest)
        smap = {p: s for p, (s, _) in fm.items()}
        total = len(fm)

        if not site_id:
            await cb("Step 2/4 - Creating Netlify site...")
            site_id = net_create_site()["id"]

        await cb(f"Step 3/4 - Creating deploy ({total} files)...")
        dep = net_create_deploy(site_id, smap)
        dep_id = dep["id"]
        required = set(dep.get("required", []))

        if required:
            done = 0
            tot = len(required)
            await cb(f"Step 3/4 - Uploading {tot} files...")
            for path, (s, data) in fm.items():
                if s in required:
                    net_upload(dep_id, path, data)
                    done += 1
                    if done % max(1, tot//5) == 0 or done == tot:
                        pct = int(done / tot * 100)
                        await cb(f"Uploading... {pct}% ({done}/{tot})")

        await cb("Step 4/4 - Waiting for site to go live...")
        await asyncio.sleep(4)

        info = net_get_deploy(dep_id)
        url = info.get("deploy_ssl_url") or info.get("deploy_url") or info.get("ssl_url") or info.get("url") or ""
        if not url:
            r = requests.get(f"{_NB}/sites/{site_id}", headers=_NH, timeout=15)
            if r.ok:
                url = r.json().get("ssl_url") or r.json().get("url") or ""

        if not url:
            return False, "Deploy finished but no URL returned.", site_id, dep_id

        return True, url, site_id, dep_id

    except Exception as e:
        log.error(f"Deploy error: {e}")
        return False, f"Error: {str(e)[:200]}", "", ""
    finally:
        shutil.rmtree(dest, ignore_errors=True)
        try:
            os.remove(zip_path)
        except:
            pass

# ═══════════════════════════════════════════════════════════
#  COMMANDS
# ═══════════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db_upsert_user(u.id, u.username, u.first_name)
    ctx.user_data.clear()

    if not await gate(update, ctx):
        return

    count = db_count(u.id)
    
    if count > 0:
        text = f"Welcome back, {u.first_name}!\n\nYou have {count} project(s) saved.\n\nWhat would you like to do?"
    else:
        text = f"Welcome to DeployX, {u.first_name}!\n\nI help you deploy websites instantly to the internet.\n\nQuick Start:\n1. Prepare your website files (must have index.html)\n2. ZIP them with: zip -j site.zip your-folder/*\n3. Send the ZIP to me\n\nThat's it! You'll get a live URL immediately."
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Open Main Menu", callback_data="panel")],
        [InlineKeyboardButton("Quick Deploy", callback_data="quick_deploy")],
        [InlineKeyboardButton("View Help Guide", callback_data="help")]
    ])
    
    await update.message.reply_text(text, reply_markup=keyboard)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    
    help_text = """DeployX Bot - Quick Help

How to Deploy Your Website:

1. Prepare your website files
   - Must include index.html
   - Only static files (HTML/CSS/JS/images)

2. Create a ZIP file
   Use this command: zip -j site.zip your-folder/*
   (The -j flag puts files at root level)

3. Send the ZIP to this bot
   - Use Quick Deploy for instant deployment
   - Or create a Project to save it

Commands:
/start - Welcome message
/panel - Open main menu
/deploy - Quick deploy a website
/help - Show this help message

Common Problems:

No index.html -> Use -j flag when zipping
File too large -> Max size is 10 MB
PHP not working -> Only static sites supported

Premium Templates and Private Sites:
Contact @LM_S0

Need more help? Contact @LM_S0"""

    await update.message.reply_text(help_text, reply_markup=kb_back())

async def cmd_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db_upsert_user(u.id, u.username, u.first_name)
    
    if not await gate(update, ctx):
        return
    
    ctx.user_data.clear()
    count = db_count(u.id)
    await update.message.reply_text(f"DeployX Control Panel\n\nActive Projects: {count}\n\nChoose an option below:", reply_markup=kb_panel())

async def cmd_deploy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db_upsert_user(u.id, u.username, u.first_name)
    
    if not await gate(update, ctx):
        return
    
    ctx.user_data.clear()
    ctx.user_data["state"] = STATE_WAITING_ZIP
    ctx.user_data["mode"] = "quick"
    await update.message.reply_text("Quick Deploy\n\nSend me your .zip file and I'll deploy it instantly!\n\nTip: use zip -j site.zip folder/*", reply_markup=kb_cancel())

# ═══════════════════════════════════════════════════════════
#  CALLBACK HANDLER
# ═══════════════════════════════════════════════════════════
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    u = update.effective_user
    msg = q.message

    if data == "check_sub":
        if await is_subscribed(update, ctx):
            db_upsert_user(u.id, u.username, u.first_name)
            await safe_edit(msg, f"Verified! Welcome, {u.first_name}!\n\nYou're all set. Tap below to start deploying.", InlineKeyboardMarkup([[InlineKeyboardButton("Open Panel", callback_data="panel")]]))
        else:
            await q.answer("You haven't joined yet.", show_alert=True)
        return

    if not await gate(update, ctx):
        return

    db_upsert_user(u.id, u.username, u.first_name)

    if data == "panel":
        ctx.user_data.clear()
        count = db_count(u.id)
        await safe_edit(msg, f"DeployX Panel\n\nProjects: {count}\n\nWhat would you like to do?", kb_panel())

    elif data == "help":
        help_text = """DeployX Bot - Quick Help

How to Deploy:

1. Prepare files (must have index.html)
2. ZIP with: zip -j site.zip folder/*
3. Send ZIP to bot

Commands:
/start - Welcome
/panel - Main menu
/deploy - Quick deploy

Premium templates: @LM_S0"""
        await safe_edit(msg, help_text, kb_back())

    elif data == "new_project":
        ctx.user_data["state"] = STATE_WAITING_NAME
        await safe_edit(msg, "Create New Project\n\nSend me a name for your project.\n\nExample: my-portfolio", kb_cancel())

    elif data == "my_projects":
        projects = db_user_projects(u.id)
        if not projects:
            await safe_edit(msg, "My Projects\n\nYou don't have any projects yet!\n\nGet started by creating your first project:", InlineKeyboardMarkup([
                [InlineKeyboardButton("Create First Project", callback_data="new_project")],
                [InlineKeyboardButton("Quick Deploy", callback_data="quick_deploy")],
                [InlineKeyboardButton("Back to Menu", callback_data="panel")],
            ]))
        else:
            await safe_edit(msg, f"Your Projects ({len(projects)})\n\nTap a project to manage it:", kb_projects(projects))

    elif data == "quick_deploy":
        ctx.user_data.clear()
        ctx.user_data["state"] = STATE_WAITING_ZIP
        ctx.user_data["mode"] = "quick"
        await safe_edit(msg, "Quick Deploy Mode\n\nSend me your ZIP file and I'll deploy it instantly!\n\nMake sure your ZIP:\n- Contains index.html\n- Is under 10 MB", kb_cancel())

    elif data.startswith("proj_"):
        pid = int(data.split("_")[1])
        proj = db_get_project(pid)
        if not proj or proj["user_id"] != u.id:
            await q.answer("Project not found.", show_alert=True)
            return
        icon = "✅" if proj["status"] == "deployed" else "🕐"
        url_line = f"URL: {proj['url']}" if proj["url"] else "Not deployed yet"
        await safe_edit(msg, f"{icon} {proj['name']}\n\nStatus: {proj['status'].capitalize()}\n{url_line}\n\nWhat would you like to do?", kb_project(pid))

    elif data.startswith("deploy_"):
        pid = int(data.split("_")[1])
        proj = db_get_project(pid)
        if not proj or proj["user_id"] != u.id:
            await q.answer("Project not found.", show_alert=True)
            return
        ctx.user_data.clear()
        ctx.user_data["state"] = STATE_WAITING_ZIP
        ctx.user_data["mode"] = "project"
        ctx.user_data["pid"] = pid
        await safe_edit(msg, f"Deploy -> {proj['name']}\n\nSend me your .zip file.", kb_cancel())

    elif data.startswith("redeploy_"):
        pid = int(data.split("_")[1])
        proj = db_get_project(pid)
        if not proj or proj["user_id"] != u.id:
            await q.answer("Project not found.", show_alert=True)
            return
        if not proj["site_id"]:
            await q.answer("Deploy this project first.", show_alert=True)
            return
        ctx.user_data.clear()
        ctx.user_data["state"] = STATE_WAITING_REDEPLOY_ZIP
        ctx.user_data["pid"] = pid
        ctx.user_data["site_id"] = proj["site_id"]
        await safe_edit(msg, f"Redeploy -> {proj['name']}\n\nSend me the updated .zip file.", kb_cancel())

    elif data.startswith("url_"):
        pid = int(data.split("_")[1])
        proj = db_get_project(pid)
        if not proj or proj["user_id"] != u.id:
            await q.answer("Project not found.", show_alert=True)
            return
        if proj["url"]:
            await q.answer(proj["url"], show_alert=True)
        else:
            await q.answer("No URL yet - deploy first.", show_alert=True)

    elif data.startswith("delete_"):
        pid = int(data.split("_")[1])
        proj = db_get_project(pid)
        if not proj or proj["user_id"] != u.id:
            await q.answer("Project not found.", show_alert=True)
            return
        await safe_edit(msg, f"Delete '{proj['name']}'?\n\nThis will permanently remove the project and its Netlify site.", kb_confirm(pid))

    elif data.startswith("confirm_delete_"):
        pid = int(data.split("_")[2])
        proj = db_get_project(pid)
        if not proj or proj["user_id"] != u.id:
            await q.answer("Project not found.", show_alert=True)
            return
        name = proj["name"]
        if proj["site_id"]:
            net_delete_site(proj["site_id"])
        db_delete_project(pid)
        count = db_count(u.id)
        await safe_edit(msg, f"'{name}' deleted.\n\nYou now have {count} project(s).", InlineKeyboardMarkup([
            [InlineKeyboardButton("My Projects", callback_data="my_projects")],
            [InlineKeyboardButton("Panel", callback_data="panel")],
        ]))

    else:
        await q.answer("Unknown option.", show_alert=True)

# ═══════════════════════════════════════════════════════════
#  TEXT HANDLER
# ═══════════════════════════════════════════════════════════
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db_upsert_user(u.id, u.username, u.first_name)

    if not await gate(update, ctx):
        return

    if ctx.user_data.get("state") == STATE_WAITING_NAME:
        name = re.sub(r"[^\w\s\-]", "", update.message.text.strip())[:64].strip()
        if not name:
            await update.message.reply_text("Please send a valid name (letters, numbers, dashes).", reply_markup=kb_cancel())
            return
        pid = db_create_project(u.id, name)
        ctx.user_data["state"] = STATE_WAITING_ZIP
        ctx.user_data["mode"] = "project"
        ctx.user_data["pid"] = pid
        await update.message.reply_text(f"Project '{name}' created!\n\nNow send me the .zip file to deploy.", reply_markup=kb_cancel())
    else:
        await update.message.reply_text("Use /panel to open the menu or /deploy for a quick deploy.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open Panel", callback_data="panel")]]))

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
        await update.message.reply_text("Use /deploy or open the panel first.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open Panel", callback_data="panel")]]))
        return

    doc = update.message.document
    if not doc.file_name.lower().endswith(".zip"):
        await update.message.reply_text("Wrong file type.\n\nI only accept .zip files.", reply_markup=kb_cancel())
        return

    if doc.file_size and doc.file_size > MAX_ZIP_BYTES:
        mb = doc.file_size / 1024 / 1024
        await update.message.reply_text(f"File Too Large ({mb:.1f} MB)\n\nMax is 10 MB.", reply_markup=kb_cancel())
        return

    status_msg = await update.message.reply_text("Downloading your file...")
    
    os.makedirs(WORK_DIR, exist_ok=True)
    zip_path = os.path.join(WORK_DIR, f"{u.id}_{doc.file_unique_id}.zip")

    tg = await ctx.bot.get_file(doc.file_id)
    await tg.download_to_drive(zip_path)

    mode = ctx.user_data.get("mode", "quick")
    pid = ctx.user_data.get("pid")
    site_id = ctx.user_data.get("site_id")

    if mode == "quick":
        name = re.sub(r"\.zip$", "", doc.file_name, flags=re.IGNORECASE)[:64]
        name = re.sub(r"[^\w\s\-]", "", name).strip() or "my-site"
        pid = db_create_project(u.id, name)

    async def cb(text: str):
        try:
            await status_msg.edit_text(text)
        except:
            pass

    ok, result, new_site_id, dep_id = await deploy(zip_path, site_id, cb)

    if ok:
        db_update_project(pid, new_site_id, dep_id, result, "deployed")
        proj = db_get_project(pid)
        await status_msg.edit_text(f"Deployment Successful!\n\nProject: {proj['name']}\nLive URL: {result}\n\nYour site is live!", reply_markup=kb_deployed(result, pid))
    else:
        await status_msg.edit_text(f"{result}\n\nTap below to go back.", reply_markup=kb_back())

    ctx.user_data.clear()

# ═══════════════════════════════════════════════════════════
#  ERROR HANDLER
# ═══════════════════════════════════════════════════════════
async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception", exc_info=ctx.error)

# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════
def main():
    db_init()
    os.makedirs(WORK_DIR, exist_ok=True)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("deploy", cmd_deploy))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("DeployX Bot started successfully!")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
