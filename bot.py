import os
import sqlite3
import threading
import pytz
import httpx
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- CONFIGURATION ---
DB_PATH = "memory.db"
FILES_DIR = Path("files")
FILES_DIR.mkdir(exist_ok=True)
scheduler = AsyncIOScheduler()

GROQ_KEY = os.environ.get("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SYSTEM_PROMPT = """You are a personal assistant that helps track packages, calculate dates, and set reminders.
You have access to the exact current date and time in Antananarivo. 
Always use this real-time information to calculate deadlines and reminders correctly."""

# --- DATABASE ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chat_id INTEGER, message_id INTEGER, type TEXT, content TEXT, timestamp TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, role TEXT, content TEXT, timestamp TEXT)''')
    conn.commit()
    conn.close()

init_db()

def save_message(user_id, chat_id, message_id, msg_type, content):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO messages (user_id, chat_id, message_id, type, content, timestamp) VALUES (?, ?, ?, ?, ?, ?)',
              (user_id, chat_id, message_id, msg_type, content, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def save_memory(user_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO memories (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)',
              (user_id, role, content, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_memories(user_id, limit=15):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT role, content FROM memories WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?', (user_id, limit))
    results = c.fetchall()
    conn.close()
    return list(reversed(results))

# --- IA GROQ (Correction de l'indentation) ---
async def ask_groq(user_id, user_message):
    memories = get_memories(user_id, limit=12)
    
    tz = pytz.timezone('Africa/Nairobi')
    now = datetime.now(tz)
    date_context = f"Today is {now.strftime('%A, %d %B %Y')}, {now.strftime('%H:%M:%S %Z')}."
    
    messages = [
        {"role": "system", "content": f"{SYSTEM_PROMPT}\nCALENDAR DATA: {date_context}\nCRITICAL: Use this date for all calculations. Do not guess."}
    ]
    for role, content in memories:
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})
    
    # Tout le bloc ci-dessous DOIT être indenté sous ask_groq
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.1-8b-instant", "messages": messages, "temperature": 0.3, "max_tokens": 800}
        )
        data = response.json()
        if "choices" in data:
            return data["choices"][0]["message"]["content"]
        return "Error"

# --- HANDLERS ---
async def start(update, context):
    await update.message.reply_text("Bot actif.")

async def handle_text(update, context):
    user_id = update.effective_user.id
    text = update.message.text
    save_message(user_id, update.effective_chat.id, update.message.message_id, "text", text)
    save_memory(user_id, "user", text)
    response = await ask_groq(user_id, text)
    save_memory(user_id, "assistant", response)
    await update.message.reply_text(response)

# --- SERVEUR & MAIN ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

def main():
    token = os.environ["BOT_TOKEN"]
    threading.Thread(target=run_web_server, daemon=True).start()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
