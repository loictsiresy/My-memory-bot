import os
import sqlite3
import threading
import re
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

GROQ_KEY = os.environ.get("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

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

def get_memories(user_id, limit=15):
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

def get_current_datetime():
    now = datetime.now()
    return {"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"),
            "weekday": DAYS[now.weekday()], "year": now.year, "month": now.month, "day": now.day}

def get_weekday(year, month, day):
    try:
        dt = datetime(year, month, day)
        return {"date": dt.strftime("%Y-%m-%d"), "weekday": DAYS[dt.weekday()], "day_of_week": dt.weekday()}
    except ValueError:
        return {"error": "Invalid date"}

def add_days(year, month, day, days_to_add):
    try:
        dt = datetime(year, month, day) + timedelta(days=days_to_add)
        return {"start_date": f"{year}-{month:02d}-{day:02d}", "days_added": days_to_add,
                "result_date": dt.strftime("%Y-%m-%d"), "result_weekday": DAYS[dt.weekday()]}
    except ValueError:
        return {"error": "Invalid date"}

def next_weekday_from_date(year, month, day, target_weekday):
    try:
        dt = datetime(year, month, day)
        target_idx = DAYS.index(target_weekday.capitalize())
        days_ahead = target_idx - dt.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        result = dt + timedelta(days=days_ahead)
        return {"from_date": dt.strftime("%Y-%m-%d"), "target_weekday": target_weekday,
                "next_occurrence": result.strftime("%Y-%m-%d"), "days_until": days_ahead}
    except (ValueError, IndexError):
        return {"error": "Invalid date or weekday"}

TOOLS = [{"type": "function", "function": {"name": "get_current_datetime",
    "description": "Get current exact date, time, weekday", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_weekday",
    "description": "Get weekday for any date", "parameters": {"type": "object", "properties": {
    "year": {"type": "integer"}, "month": {"type": "integer"}, "day": {"type": "integer"}},
    "required": ["year", "month", "day"]}}},
    {"type": "function", "function": {"name": "add_days",
    "description": "Add days to a date", "parameters": {"type": "object", "properties": {
    "year": {"type": "integer"}, "month": {"type": "integer"}, "day": {"type": "integer"},
    "days_to_add": {"type": "integer"}}, "required": ["year", "month", "day", "days_to_add"]}}},
    {"type": "function", "function": {"name": "next_weekday_from_date",
    "description": "Find next occurrence of a weekday on or after a date", "parameters": {"type": "object", "properties": {
    "year": {"type": "integer"}, "month": {"type": "integer"}, "day": {"type": "integer"},
    "target_weekday": {"type": "string"}}, "required": ["year", "month", "day", "target_weekday"]}}}]

def execute_tool(name, arguments):
    if name == "get_current_datetime":
        return get_current_datetime()
    elif name == "get_weekday":
        return get_weekday(arguments["year"], arguments["month"], arguments["day"])
    elif name == "add_days":
        return add_days(arguments["year"], arguments["month"], arguments["day"], arguments["days_to_add"])
    elif name == "next_weekday_from_date":
        return next_weekday_from_date(arguments["year"], arguments["month"], arguments["day"], arguments["target_weekday"])
    return {"error": "Unknown tool"}

SYSTEM_PROMPT = """You are a personal assistant that tracks packages and calculates dates.

You have access to date calculation tools. ALWAYS use them to verify dates.

Your forwarder rules:
- A203 and EXP: receive daily. Thu-Sat received -> ship Monday. Sun-Wed received -> ship Thursday. Then 4-5 days transit.
- SEA: 45-75 days after receipt.
- Battery: receives Sun-Sat, ships next Monday only, then 15 days transit.

When calculating:
1. Use get_weekday() to verify what day the receipt date is
2. Use next_weekday_from_date() to find the ship day
3. Use add_days() to calculate delivery date
4. Include [REMINDER:YYYY-MM-DD HH:MM:description] for reminders

Be concise. Respond in user language."""

async def ask_groq_with_tools(user_id, user_message):
    memories = get_memories(user_id, limit=10)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for role, content in memories:
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.1-8b-instant", "messages": messages, "tools": TOOLS,
                  "tool_choice": "auto", "temperature": 0.2, "max_tokens": 1000})
        data = response.json()
        if "choices" not in data:
            return f"AI error: {data.get('error', 'Unknown error')}"
        message = data["choices"][0]["message"]

        if "tool_calls" in message and message["tool_calls"]:
            messages.append({"role": "assistant", "content": message.get("content", ""),
                             "tool_calls": message["tool_calls"]})
            for tool_call in message["tool_calls"]:
                function_name = tool_call["function"]["name"]
                arguments = json.loads(tool_call["function"]["arguments"])
                result = execute_tool(function_name, arguments)
                messages.append({"role": "tool", "tool_call_id": tool_call["id"],
                                 "content": json.dumps(result)})

            response2 = await client.post(GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
                json={"model": "llama-3.1-8b-instant", "messages": messages, "temperature": 0.2, "max_tokens": 1000})
            data2 = response2.json()
            if "choices" in data2:
                return data2["choices"][0]["message"]["content"]
            return message.get("content", "Got it!")
        return message.get("content", "Got it!")

def extract_reminder(text):
    match = re.search(r'\[REMINDER:(\d{4}-\d{2}-\d{2} \d{2}:\d{2}):([^\]]+)\]', text)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M"), match.group(2)
        except ValueError:
            pass
    return None, None

def clean_response(text):
    return re.sub(r'\[REMINDER:[^\]]+\]', '', text).strip()

async def start(update, context):
    await update.message.reply_text(
        "AI Memory Bot with Groq + Python Tools!

"
        "I understand natural language and calculate dates perfectly.

"
        "Just talk to me:
"
        "A203 got my package on 18 July
"
        "Remind me to call mom tomorrow 3pm
"
        "What did I say about the meeting?

"
        "Commands:
"
        "/last - Recent items
"
        "/search - Find messages
"
        "/delete - Delete by ID
"
        "/memory - What I remember")

async def handle_text(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    text = update.message.text

    save_message(user_id, chat_id, message_id, "text", text)
    save_memory(user_id, "user", text)

    if update.message.reply_to_message and "delete" in text.lower():
        orig = update.message.reply_to_message.message_id
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT id FROM messages WHERE message_id = ? AND user_id = ?', (orig, user_id))
        r = c.fetchone()
        conn.close()
        if r and delete(r[0], user_id):
            await update.message.reply_text("Deleted.")
        else:
            await update.message.reply_text("Not found.")
        return

    try:
        ai_response = await ask_groq_with_tools(user_id, text)
    except Exception as e:
        await update.message.reply_text(f"AI error: {str(e)[:100]}")
        return

    save_memory(user_id, "assistant", ai_response)

    rt, desc = extract_reminder(ai_response)
    if rt and desc:
        msg_db_id = save_message(user_id, chat_id, message_id, "reminder", desc)
        save_reminder(user_id, msg_db_id, rt, desc)
        job_id = f"r_{user_id}_{msg_db_id}_{rt.timestamp()}"
        scheduler.add_job(send_reminder, DateTrigger(run_date=rt), args=[user_id, msg_db_id, desc], id=job_id, replace_existing=True)

    clean = clean_response(ai_response)
    if clean:
        await update.message.reply_text(clean)
    else:
        await update.message.reply_text("Got it!")

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
    save_memory(user_id, "user", f"[Photo: {caption}]" if caption else "[Photo sent]")
    await update.message.reply_text(f"Photo saved! ID: {msg_db_id}")

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
    await update.message.reply_text(f"File saved! ID: {msg_db_id}")

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
    await update.message.reply_text(f"Voice saved! ID: {msg_db_id}")

async def send_reminder(user_id, msg_id, description):
    from telegram import Bot
    bot = Bot(token=os.environ["BOT_TOKEN"])
    save_memory(user_id, "system", f"[Reminder: {description}]")
    await bot.send_message(user_id, f"Reminder: {description}")

async def last_cmd(update, context):
    user_id = update.effective_user.id
    limit = int(context.args[0]) if context.args else 5
    results = search(user_id, limit=limit)
    if not results:
        await update.message.reply_text("No messages.")
        return
    text = f"Last {len(results)}:

"
    for r in results:
        msg_id, _, _, _, msg_type, content, _, caption, timestamp, _ = r
        ts = datetime.fromisoformat(timestamp).strftime("%d/%m %H:%M")
        preview = (caption or content or msg_type)[:60]
        text += f"{msg_id} | {ts} | {preview}...
"
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
    text = "Search results:

"
    for r in results[:10]:
        msg_id, _, _, _, msg_type, content, _, caption, timestamp, _ = r
        ts = datetime.fromisoformat(timestamp).strftime("%d/%m %H:%M")
        preview = (caption or content or msg_type)[:50]
        text += f"ID {msg_id} | {ts} | {preview}...
"
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

async def memory_cmd(update, context):
    user_id = update.effective_user.id
    memories = get_memories(user_id, limit=10)
    if not memories:
        await update.message.reply_text("I do not remember anything yet!")
        return
    text = "What I remember:

"
    for role, content in memories:
        prefix = "You: " if role == "user" else "Me: " if role == "assistant" else "System: "
        text += f"{prefix}{content[:120]}...

"
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


