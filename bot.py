import os
import sqlite3
import threading
import json
from datetime import datetime, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

DB_PATH = "memory.db"
FILES_DIR = Path("files")
FILES_DIR.mkdir(exist_ok=True)
scheduler = AsyncIOScheduler()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# ============== DATABASE ==============

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, chat_id INTEGER, message_id INTEGER,
        type TEXT, content TEXT, file_path TEXT, caption TEXT,
        timestamp TEXT, is_deleted INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, message_id INTEGER, reminder_time TEXT,
        description TEXT, is_sent INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, role TEXT, content TEXT, timestamp TEXT)''')
    conn.commit()
    conn.close()

init_db()

def get_db():
    return sqlite3.connect(DB_PATH)

def save_message(user_id, chat_id, message_id, msg_type, content, file_path=None, caption=None):
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO messages (user_id, chat_id, message_id, type, content, file_path, caption, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (user_id, chat_id, message_id, msg_type, content, file_path, caption, datetime.now().isoformat()))
    conn.commit()
    last_id = c.lastrowid
    conn.close()
    return last_id

def save_memory(user_id, role, content):
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO memories (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)',
        (user_id, role, content, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_memories(user_id, limit=20):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT role, content FROM memories WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?', (user_id, limit))
    results = c.fetchall()
    conn.close()
    return list(reversed(results))

def save_reminder(user_id, msg_id, time, desc):
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO reminders (user_id, message_id, reminder_time, description) VALUES (?, ?, ?, ?)',
        (user_id, msg_id, time.isoformat(), desc))
    conn.commit()
    last_id = c.lastrowid
    conn.close()
    return last_id

def search(user_id, query=None, limit=10):
    conn = get_db()
    c = conn.cursor()
    sql = 'SELECT * FROM messages WHERE user_id = ? AND is_deleted = 0'
    params = [user_id]
    if query:
        sql += ' AND (content LIKE ? OR caption LIKE ?)'
        params.extend([f'%{query}%', f'%{query}%'])
    sql += ' ORDER BY timestamp DESC LIMIT ?'
    params.append(limit)
    c.execute(sql, params)
    results = c.fetchall()
    conn.close()
    return results

def delete(msg_id, user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE messages SET is_deleted = 1 WHERE id = ? AND user_id = ?', (msg_id, user_id))
    conn.commit()
    deleted = c.rowcount > 0
    conn.close()
    return deleted

# ============== GROQ AI ==============

SYSTEM_PROMPT = """You are a personal memory and tracking assistant. You help the user remember information, track packages, calculate dates, and set reminders.

Your capabilities:
1. Remember everything the user tells you
2. Track packages from different forwarders/shipping companies
3. Calculate delivery dates based on rules the user provides
4. Extract dates from natural language
5. Suggest when to remind the user
6. Understand context from previous messages

When the user mentions a package, forwarder, or delivery:
- Extract the forwarder name
- Extract the receipt/date information
- Calculate expected delivery if rules are known
- Ask for rules if unknown
- Suggest a reminder date

When responding, be concise but helpful. Use the user's language.

If you detect a reminder should be set, include this exact format at the end of your response:
[REMINDER:YYYY-MM-DD HH:MM:description]

For example:
[REMINDER:2026-09-01 09:00:Check if A203 package arrived]

If no reminder is needed, don't include the tag."""

async def ask_groq(user_id, user_message):
    memories = get_memories(user_id, limit=15)
    
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for role, content in memories:
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})
    
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": 800
            }
        )
        data = response.json()
        return data["choices"][0]["message"]["content"]

def extract_reminder(text):
    import re
    match = re.search(r'\[REMINDER:(\d{4}-\d{2}-\d{2} \d{2}:\d{2}):([^\]]+)\]', text)
    if match:
        time_str = match.group(1)
        desc = match.group(2)
        try:
            rt = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
            return rt, desc
        except ValueError:
            pass
    return None, None

def clean_response(text):
    return re.sub(r'\[REMINDER:[^\]]+\]', '', text).strip()

# ============== HANDLERS ==============

async def start(update, context):
    await update.message.reply_text(
        "🧠 AI Memory Bot activated!\n\n"
        "I remember everything and think for you.\n\n"
        "Just talk to me naturally:\n"
        "• 'A203 got my package on 18 July'\n"
        "• 'Remind me to call mom tomorrow 3pm'\n"
        "• 'What did I say about the meeting?'\n"
        "• 'Delete that last message'\n\n"
        "Commands:\n"
        "/last - Recent items\n"
        "/search - Find messages\n"
        "/delete - Delete by ID\n"
        "/resend - Resend by ID\n"
        "/memory - What I remember about you")

async def handle_text(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    text = update.message.text
    
    # Save user message
    save_message(user_id, chat_id, message_id, "text", text)
    save_memory(user_id, "user", text)
    
    # Simple commands that don't need AI
    if text.startswith("/"):
        return
    
    if update.message.reply_to_message and "delete" in text.lower():
        orig = update.message.reply_to_message.message_id
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT id FROM messages WHERE message_id = ? AND user_id = ?', (orig, user_id))
        r = c.fetchone()
        conn.close()
        if r and delete(r[0], user_id):
            await update.message.reply_text("🗑️ Deleted.")
        else:
            await update.message.reply_text("❌ Not found.")
        return
    
    # Get AI response
    try:
        ai_response = await ask_groq(user_id, text)
    except Exception as e:
        await update.message.reply_text(f"AI error: {str(e)[:100]}")
        return
    
    # Save AI response
    save_memory(user_id, "assistant", ai_response)
    
    # Check for reminder in AI response
    rt, desc = extract_reminder(ai_response)
    if rt and desc:
        msg_db_id = save_message(user_id, chat_id, message_id, "reminder", desc)
        save_reminder(user_id, msg_db_id, rt, desc)
        job_id = f"r_{user_id}_{msg_db_id}_{rt.timestamp()}"
        scheduler.add_job(send_reminder, DateTrigger(run_date=rt), args=[user_id, msg_db_id, desc], id=job_id, replace_existing=True)
    
    # Send clean response to user
    clean = clean_response(ai_response)
    if clean:
        await update.message.reply_text(clean)
    else:
        await update.message.reply_text("✅ Got it!")

async def handle_photo(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    filename = f"photo_{user_id}_{message_id}_{int(datetime.now().timestamp())}.jpg"
    file_path = FILES_DIR / filename
    await file.download_to_drive(file_path)
    caption = update.message.caption or ""
    msg_db_id = save_message(user_id, chat_id, message_id, "photo", photo.file_id, str(file_path), caption)
    
    # Also tell AI about the photo
    desc = f"[User sent a photo"
    if caption:
        desc += f" with caption: {caption}"
    desc += "]"
    save_memory(user_id, "user", desc)
    
    await update.message.reply_text(f"📸 Photo saved! ID: {msg_db_id}")

async def handle_document(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    doc = update.message.document
    file = await context.bot.get_file(doc.file_id)
    filename = f"doc_{user_id}_{message_id}_{doc.file_name or 'file'}"
    file_path = FILES_DIR / filename
    await file.download_to_drive(file_path)
    caption = update.message.caption or ""
    msg_db_id = save_message(user_id, chat_id, message_id, "document", doc.file_id, str(file_path), caption)
    await update.message.reply_text(f"📄 File saved! ID: {msg_db_id}")

async def handle_voice(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    filename = f"voice_{user_id}_{message_id}.ogg"
    file_path = FILES_DIR / filename
    await file.download_to_drive(file_path)
    msg_db_id = save_message(user_id, chat_id, message_id, "voice", voice.file_id, str(file_path))
    await update.message.reply_text(f"🎤 Voice saved! ID: {msg_db_id}")

async def send_reminder(user_id, msg_id, description):
    from telegram import Bot
    bot = Bot(token=os.environ["BOT_TOKEN"])
    
    # Also tell AI about the reminder
    save_memory(user_id, "system", f"[Reminder triggered: {description}]")
    
    await bot.send_message(user_id, f"⏰ Reminder!\n\n{description}")

async def last_cmd(update, context):
    user_id = update.effective_user.id
    limit = int(context.args[0]) if context.args else 5
    results = search(user_id, limit=limit)
    if not results:
        await update.message.reply_text("No saved messages.")
        return
    text = f"Last {len(results)} items:\n\n"
    for r in results:
        msg_id, _, _, _, msg_type, content, _, caption, timestamp, _ = r
        ts = datetime.fromisoformat(timestamp).strftime("%d/%m %H:%M")
        preview = (caption or content or msg_type)[:60]
        text += f"{msg_id} | {ts} | {preview}...\n"
    await update.message.reply_text(text)

async def search_cmd(update, context):
    user_id = update.effective_user.id
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("Usage: /search keyword")
        return
    results = search(user_id, query=query)
    if not results:
        await update.message.reply_text("Nothing found.")
        return
    text = "Search results:\n\n"
    for r in results[:10]:
        msg_id, _, _, _, msg_type, content, _, caption, timestamp, _ = r
        ts = datetime.fromisoformat(timestamp).strftime("%d/%m %H:%M")
        preview = (caption or content or msg_type)[:50]
        text += f"ID {msg_id} | {ts} | {preview}...\n"
    await update.message.reply_text(text)

async def delete_cmd(update, context):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /delete ID")
        return
    try:
        msg_id = int(context.args[0])
        if delete(msg_id, user_id):
            await update.message.reply_text(f"Deleted {msg_id}.")
        else:
            await update.message.reply_text("Not found.")
    except ValueError:
        await update.message.reply_text("Invalid ID.")

async def resend_cmd(update, context):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /resend ID")
        return
    try:
        msg_id = int(context.args[0])
        msg = search(user_id, limit=100)
        found = None
        for m in msg:
            if m[0] == msg_id:
                found = m
                break
        if not found:
            await update.message.reply_text("Not found.")
            return
        msg_type = found[4]
        content = found[5]
        file_path = found[6]
        caption = found[7] or "Resent"
        if msg_type == "photo" and file_path and Path(file_path).exists():
            with open(file_path, "rb") as f:
                await update.message.reply_photo(photo=f, caption=caption)
        elif msg_type == "document" and file_path and Path(file_path).exists():
            with open(file_path, "rb") as f:
                await update.message.reply_document(document=f, caption=caption)
        elif msg_type == "voice" and file_path and Path(file_path).exists():
            with open(file_path, "rb") as f:
                await update.message.reply_voice(voice=f)
        elif content:
            await update.message.reply_text(content)
        else:
            await update.message.reply_text("Cannot resend.")
    except ValueError:
        await update.message.reply_text("Invalid ID.")

async def memory_cmd(update, context):
    user_id = update.effective_user.id
    memories = get_memories(user_id, limit=10)
    if not memories:
        await update.message.reply_text("I don't remember anything yet!")
        return
    text = "What I remember:\n\n"
    for role, content in memories:
        prefix = "🧑 You: " if role == "user" else "🤖 Me: " if role == "assistant" else "⚙️ "
        text += f"{prefix}{content[:100]}...\n\n"
    await update.message.reply_text(text)

async def error_handler(update, context):
    import traceback
    print(f"Error: {context.error}")
    traceback.print_exc()
    if update and update.effective_message:
        await update.effective_message.reply_text("Something went wrong. Try again!")

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def log_message(self, format, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

def main():
    token = os.environ["BOT_TOKEN"]
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("last", last_cmd))
    application.add_handler(CommandHandler("search", search_cmd))
    application.add_handler(CommandHandler("delete", delete_cmd))
    application.add_handler(CommandHandler("resend", resend_cmd))
    application.add_handler(CommandHandler("memory", memory_cmd))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)
    scheduler.start()
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
        user_id INTEGER,
        chat_id INTEGER,
        message_id INTEGER,
        type TEXT,
        content TEXT,
        file_path TEXT,
        caption TEXT,
        timestamp TEXT,
        is_deleted INTEGER DEFAULT 0
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        message_id INTEGER,
        reminder_time TEXT,
        label TEXT,
        description TEXT,
        is_sent INTEGER DEFAULT 0
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS labels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        label_name TEXT,
        delay_seconds INTEGER,
        created_at TEXT
    )''')
    
    conn.commit()
    conn.close()

init_db()

# Scheduler
scheduler = AsyncIOScheduler()

def get_db():
    return sqlite3.connect(DB_PATH)

def save_message(user_id, chat_id, message_id, msg_type, content, file_path=None, caption=None):
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO messages 
        (user_id, chat_id, message_id, type, content, file_path, caption, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (user_id, chat_id, message_id, msg_type, content, file_path, caption, datetime.now().isoformat()))
    conn.commit()
    last_id = c.lastrowid
    conn.close()
    return last_id

def get_message_by_id(msg_id, user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT * FROM messages WHERE id = ? AND user_id = ? AND is_deleted = 0''', (msg_id, user_id))
    result = c.fetchone()
    conn.close()
    return result

def search_messages(user_id, query=None, date=None, msg_type=None, limit=10):
    conn = get_db()
    c = conn.cursor()
    
    sql = '''SELECT * FROM messages WHERE user_id = ? AND is_deleted = 0'''
    params = [user_id]
    
    if query:
        sql += ' AND (content LIKE ? OR caption LIKE ?)'
        params.extend([f'%{query}%', f'%{query}%'])
    if date:
        sql += ' AND timestamp LIKE ?'
        params.append(f'{date}%')
    if msg_type:
        sql += ' AND type = ?'
        params.append(msg_type)
    
    sql += ' ORDER BY timestamp DESC LIMIT ?'
    params.append(limit)
    
    c.execute(sql, params)
    results = c.fetchall()
    conn.close()
    return results

def delete_message(msg_id, user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('''UPDATE messages SET is_deleted = 1 WHERE id = ? AND user_id = ?''', (msg_id, user_id))
    conn.commit()
    deleted = c.rowcount > 0
    conn.close()
    return deleted

def save_label(user_id, label_name, delay_seconds):
    conn = get_db()
    c = conn.cursor()
    c.execute('''DELETE FROM labels WHERE user_id = ? AND label_name = ?''', (user_id, label_name))
    c.execute('''INSERT INTO labels (user_id, label_name, delay_seconds, created_at)
        VALUES (?, ?, ?, ?)''', (user_id, label_name, delay_seconds, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_label(user_id, label_name):
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT delay_seconds FROM labels WHERE user_id = ? AND label_name = ?''', (user_id, label_name))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def save_reminder(user_id, message_id, reminder_time, label=None, description=None):
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO reminders (user_id, message_id, reminder_time, label, description)
        VALUES (?, ?, ?, ?, ?)''',
        (user_id, message_id, reminder_time.isoformat(), label, description))
    conn.commit()
    last_id = c.lastrowid
    conn.close()
    return last_id

def parse_time(text):
    text = text.lower().strip()
    now = datetime.now()
    
    try:
        if len(text) == 10:
            dt = datetime.strptime(text, "%Y-%m-%d")
            return dt.replace(hour=9, minute=0)
        return datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        pass
    
    if text.startswith("in "):
        parts = text[3:].split()
        if len(parts) == 2:
            num = int(parts[0])
            unit = parts[1].rstrip('s')
            if unit in ("minute", "min"):
                return now + timedelta(minutes=num)
            elif unit in ("hour", "hr"):
                return now + timedelta(hours=num)
            elif unit in ("day",):
                return now + timedelta(days=num)
            elif unit in ("week",):
                return now + timedelta(weeks=num)
    
    if "tomorrow" in text:
        tomorrow = now + timedelta(days=1)
        time_part = text.replace("tomorrow", "").strip()
        if time_part:
            try:
                t = datetime.strptime(time_part, "%H:%M").time()
                return datetime.combine(tomorrow.date(), t)
            except:
                pass
        return tomorrow.replace(hour=9, minute=0)
    
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, day in enumerate(days):
        if day in text:
            target = now + timedelta(days=1)
            while target.weekday() != i:
                target += timedelta(days=1)
            time_part = text.replace(day, "").replace("next", "").strip()
            if time_part:
                try:
                    t = datetime.strptime(time_part, "%H:%M").time()
                    return datetime.combine(target.date(), t)
                except:
                    pass
            return target.replace(hour=9, minute=0)
    
    return None

def format_duration(seconds):
    if seconds < 3600:
        return f"{seconds // 60} minutes"
    elif seconds < 86400:
        return f"{seconds // 3600} hours"
    elif seconds < 604800:
        return f"{seconds // 86400} days"
    else:
        return f"{seconds // 604800} weeks"

# ============== HANDLERS ==============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧠 *Memory Bot activated!*\n\n"
        "I remember everything you send me.\n\n"
        "*Commands:*\n"
        "/remind - Set a reminder\n"
        "/search - Find saved messages\n"
        "/last - Show recent items\n"
        "/labels - Manage your labels\n"
        "/delete - Delete a saved item\n\n"
        "Just send me text, photos, files, or voice messages — I'll save them all!",
        parse_mode="Markdown"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*How to use me:*\n\n"
        "📥 *Save:* Just send anything — text, photo, file, voice\n\n"
        "⏰ *Remind:* Reply to any message with 'remind me [when]' or use /remind\n"
        "Examples:\n"
        "• 'remind me tomorrow 3pm'\n"
        "• 'remind me in 2 hours'\n"
        "• 'remind me 2026-07-25 09:00'\n\n"
        "🏷️ *Labels:* When saving, say 'label: [name]' and I'll learn the timing\n"
        "Example: 'Meeting notes, label: urgent' → I'll ask how long 'urgent' means\n\n"
        "🔍 *Find:* /search [keyword] or /last [number]\n\n"
        "🗑️ *Delete:* Reply 'delete this' to any message, or use /delete [id]",
        parse_mode="Markdown"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    text = update.message.text
    
    if update.message.reply_to_message and "delete" in text.lower():
        orig_msg_id = update.message.reply_to_message.message_id
        conn = get_db()
        c = conn.cursor()
        c.execute('''SELECT id FROM messages WHERE message_id = ? AND user_id = ?''', 
                  (orig_msg_id, user_id))
        result = c.fetchone()
        conn.close()
        
        if result:
            delete_message(result[0], user_id)
            await update.message.reply_text("🗑️ Deleted from my memory.")
        else:
            await update.message.reply_text("❌ Couldn't find that message to delete.")
        return
    
    if update.message.reply_to_message and "remind" in text.lower():
        orig_msg_id = update.message.reply_to_message.message_id
        conn = get_db()
        c = conn.cursor()
        c.execute('''SELECT id, content, caption FROM messages WHERE message_id = ? AND user_id = ?''',
                  (orig_msg_id, user_id))
        result = c.fetchone()
        conn.close()
        
        if result:
            msg_db_id, content, caption = result
            desc = caption or content or "reminder"
            
            time_text = text.lower().replace("remind me", "").replace("remind", "").strip()
            reminder_time = parse_time(time_text)
            
            if reminder_time:
                save_reminder(user_id, msg_db_id, reminder_time, description=desc)
                job_id = f"reminder_{user_id}_{msg_db_id}_{reminder_time.timestamp()}"
                scheduler.add_job(
                    send_reminder,
                    DateTrigger(run_date=reminder_time),
                    args=[user_id, msg_db_id, desc],
                    id=job_id,
                    replace_existing=True
                )
                await update.message.reply_text(f"⏰ Reminder set for {reminder_time.strftime('%Y-%m-%d %H:%M')}")
            else:
                await update.message.reply_text("❓ I didn't understand the time. Try:\n• 'tomorrow 3pm'\n• 'in 2 hours'\n• '2026-07-25 14:00'")
        return
    
    label = None
    if "label:" in text.lower():
        parts = text.lower().split("label:")
        main_text = parts[0].strip()
        label_part = parts[1].strip().split()[0] if len(parts) > 1 else None
        if label_part:
            label = label_part
            delay = get_label(user_id, label)
            if delay is None:
                context.user_data["pending_label"] = {"name": label, "text": main_text}
                await update.message.reply_text(
                    f"🏷️ New label '*{label}*'!\n"
                    f"How long should '{label}' reminders wait?\n"
                    f"Reply with: '2 hours', '3 days', '1 week', etc.",
                    parse_mode="Markdown"
                )
                return
    
    msg_db_id = save_message(user_id, chat_id, message_id, "text", text, caption=label)
    
    if label:
        delay = get_label(user_id, label)
        if delay:
            reminder_time = datetime.now() + timedelta(seconds=delay)
            save_reminder(user_id, msg_db_id, reminder_time, label=label, description=text[:100])
            job_id = f"reminder_{user_id}_{msg_db_id}_{reminder_time.timestamp()}"
            scheduler.add_job(
                send_reminder,
                DateTrigger(run_date=reminder_time),
                args=[user_id, msg_db_id, text[:100]],
                id=job_id,
                replace_existing=True
            )
            await update.message.reply_text(
                f"✅ Saved with label '{label}'.\n"
                f"⏰ I'll remind you in {format_duration(delay)} ({reminder_time.strftime('%H:%M %d/%m')})"
            )
            return
    
    await update.message.reply_text("✅ Saved to memory!")

async def handle_label_definition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.lower().strip()
    
    if "pending_label" not in context.user_data:
        return
    
    pending = context.user_data["pending_label"]
    label_name = pending["name"]
    original_text = pending["text"]
    
    parts = text.split()
    if len(parts) == 2:
        try:
            num = int(parts[0])
            unit = parts[1].rstrip('s')
            seconds = 0
            if unit in ("minute", "min"):
                seconds = num * 60
            elif unit in ("hour", "hr"):
                seconds = num * 3600
            elif unit in ("day",):
                seconds = num * 86400
            elif unit in ("week",):
                seconds = num * 604800
            
            if seconds > 0:
                save_label(user_id, label_name, seconds)
                del context.user_data["pending_label"]
                
                msg_db_id = save_message(user_id, update.effective_chat.id, 
                                         update.message.message_id, "text", 
                                         original_text, caption=label_name)
                
                reminder_time = datetime.now() + timedelta(seconds=seconds)
                save_reminder(user_id, msg_db_id, reminder_time, label=label_name, description=original_text[:100])
                job_id = f"reminder_{user_id}_{msg_db_id}_{reminder_time.timestamp()}"
                scheduler.add_job(
                    send_reminder,
                    DateTrigger(run_date=reminder_time),
                    args=[user_id, msg_db_id, original_text[:100]],
                    id=job_id,
                    replace_existing=True
                )
                
                await update.message.reply_text(
                    f"✅ Label '{label_name}' = {format_duration(seconds)}\n"
                    f"⏰ Reminder set for {reminder_time.strftime('%H:%M %d/%m')}"
                )
                return
        except ValueError:
            pass
    
    await update.message.reply_text("❓ Please reply with format: '2 hours', '3 days', '1 week'")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    ext = ".jpg"
    filename = f"photo_{user_id}_{message_id}_{int(datetime.now().timestamp())}{ext}"
    file_path = FILES_DIR / filename
    await file.download_to_drive(file_path)
    
    caption = update.message.caption or ""
    msg_db_id = save_message(user_id, chat_id, message_id, "photo", photo.file_id, 
                             str(file_path), caption=caption)
    
    await update.message.reply_text(f"📸 Photo saved! (ID: {msg_db_id})")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    
    doc = update.message.document
    file = await context.bot.get_file(doc.file_id)
    filename = f"doc_{user_id}_{message_id}_{doc.file_name or 'file'}"
    file_path = FILES_DIR / filename
    await file.download_to_drive(file_path)
    
    caption = update.message.caption or ""
    msg_db_id = save_message(user_id, chat_id, message_id, "document", doc.file_id,
                             str(file_path), caption=caption)
    
    await update.message.reply_text(f"📄 File saved! (ID: {msg_db_id})")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    filename = f"voice_{user_id}_{message_id}.ogg"
    file_path = FILES_DIR / filename
    await file.download_to_drive(file_path)
    
    msg_db_id = save_message(user_id, chat_id, message_id, "voice", voice.file_id,
                             str(file_path))
    
    await update.message.reply_text(f"🎤 Voice message saved! (ID: {msg_db_id})")

async def send_reminder(user_id, msg_id, description):
    from telegram import Bot
    bot = Bot(token=os.environ["BOT_TOKEN"])
    
    msg = get_message_by_id(msg_id, user_id)
    if not msg:
        await bot.send_message(user_id, f"⏰ Reminder: {description}\n(Original message was deleted)")
        return
    
    msg_type = msg[4]
    content = msg[5]
    file_path = msg[6]
    caption = msg[7] or "Reminder!"
    
    await bot.send_message(user_id, f"⏰ *Reminder!*\n\n{caption}", parse_mode="Markdown")
    
    if msg_type == "photo" and file_path and Path(file_path).exists():
        with open(file_path, "rb") as f:
            await bot.send_photo(user_id, photo=f, caption="Your saved photo")
    elif msg_type == "document" and file_path and Path(file_path).exists():
        with open(file_path, "rb") as f:
            await bot.send_document(user_id, document=f, caption="Your saved file")
    elif msg_type == "voice" and file_path and Path(file_path).exists():
        with open(file_path, "rb") as f:
            await bot.send_voice(user_id, voice=f)
    elif content:
        await bot.send_message(user_id, f"📝 Original text:\n{content[:4000]}")

async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = " ".join(context.args) if context.args else ""
    
    if not query:
        await update.message.reply_text("Usage: /search [keyword]\nOr: /search date:2026-07-18")
        return
    
    if query.startswith("date:"):
        date = query.replace("date:", "").strip()
        results = search_messages(user_id, date=date)
    else:
        results = search_messages(user_id, query=query)
    
    if not results:
        await update.message.reply_text("🔍 Nothing found.")
        return
    
    text = "🔍 *Search results:*\n\n"
    for r in results[:10]:
        msg_id, _, _, _, msg_type, content, _, caption, timestamp, _ = r
        ts = datetime.fromisoformat(timestamp).strftime("%d/%m %H:%M")
        preview = (caption or content or msg_type)[:50]
        text += f"• ID `{msg_id}` | {ts} | {preview}...\n"
    
    await update.message.reply_text(text, parse_mode="Markdown")

async def last_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    limit = int(context.args[0]) if context.args else 5
    
    results = search_messages(user_id, limit=limit)
    
    if not results:
        await update.message.reply_text("📭 No saved messages yet.")
        return
    
    text = f"📋 *Last {len(results)} items:*\n\n"
    for r in results:
        msg_id, _, _, _, msg_type, content, _, caption, timestamp, _ = r
        ts = datetime.fromisoformat(timestamp).strftime("%d/%m %H:%M")
        preview = (caption or content or msg_type)[:60]
        text += f"`{msg_id}` | {ts} | {preview}...\n"
    
    await update.message.reply_text(text, parse_mode="Markdown")

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not context.args:
        await update.message.reply_text("Usage: /delete [message ID]\nFind IDs with /last or /search")
        return
    
    try:
        msg_id = int(context.args[0])
        if delete_message(msg_id, user_id):
            await update.message.reply_text(f"🗑️ Message {msg_id} deleted from memory.")
        else:
            await update.message.reply_text("❌ Message not found or already deleted.")
    except ValueError:
                await update.message.reply_text("❌ Please provide a valid message ID.")

# ============== MAIN ==============

def main():
    # Remplace 'TON_TOKEN_ICI' par la variable d'environnement ou le token réel
    TOKEN = os.environ.get("BOT_TOKEN")
    
    if not TOKEN:
        logger.error("BOT_TOKEN non trouvé dans les variables d'environnement")
        return

    application = Application.builder().token(TOKEN).build()

    # Enregistrement des commandes
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("search", search_cmd))
    application.add_handler(CommandHandler("last", last_cmd))
    application.add_handler(CommandHandler("delete", delete_cmd))

    # Gestion des messages et contenus
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Démarrage du scheduler et du bot
    scheduler.start()
    application.run_polling()

if __name__ == "__main__":
    main()
    
