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

# --- SYSTEM PROMPT ---
SYSTEM_PROMPT = """You are a personal assistant that helps track packages, calculate dates, and set reminders.
You have access to the exact current date and time in Antananarivo. 
Always use this real-time information to calculate deadlines and reminders correctly."""

# --- DATABASE INITIALIZATION ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chat_id INTEGER, 
        message_id INTEGER, type TEXT, content TEXT, file_path TEXT, 
        caption TEXT, timestamp TEXT, is_deleted INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, message_id INTEGER, 
        reminder_time TEXT, description TEXT, is_sent INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, role TEXT, 
        content TEXT, timestamp TEXT)''')
    conn.commit()
    conn.close()

init_db()

# --- FONCTIONS UTILES ---
def get_db():
    return sqlite3.connect(DB_PATH)

def save_message(user_id, chat_id, message_id, msg_type, content):
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO messages (user_id, chat_id, message_id, type, content, timestamp) VALUES (?, ?, ?, ?, ?, ?)',
        (user_id, chat_id, message_id, msg_type, content, datetime.now().isoformat()))
    conn.commit()
    conn.close()

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

# --- IA GROQ AVEC TEMPS RÉEL ---
async def ask_groq(user_id, user_message):
    memories = get_memories(user_id, limit=12)
    
    # Calcul précis du jour et de la date en Python
    tz = pytz.timezone('Africa/Nairobi')
    now = datetime.now(tz)
    date_context = f"Today is {now.strftime('%A, %d %B %Y')}, {now.strftime('%H:%M:%S %Z')}."
    
    messages = [
        {"role": "system", "content": f"{SYSTEM_PROMPT}\nCALENDAR DATA: {date_context}\nCRITICAL: Use this date for all calculations. Do not guess."}
    ]
    for role, content in memories:
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})
    
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.1-8b-instant", "messages": messages, "temperature": 0.3, "max_tokens": 800}
        )
        data = response.json()
        return data["choices"][0]["message"]["content"] if "choices" in data else "Error"

# --- HANDLERS ---
async def start(update, context):
    await update.message.reply_text("Bot actif et synchronisé avec le calendrier d'Antananarivo.")

async def handle_text(update, context):
    user_id = update.effective_user.id
    text = update.message.text
    save_message(user_id, update.effective_chat.id, update.message.message_id, "text", text)
    save_memory(user_id, "user", text)
    response = await ask_groq(user_id, text)
    save_memory(user_id, "assistant", response)
    await update.message.reply_text(response)

# --- SERVEUR SANTÉ & MAIN ---
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
    
    # drop_pending_updates=True résout le conflit Telegram
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.1-8b-instant", "messages": messages, "temperature": 0.3, "max_tokens": 800}
        )
        data = response.json()
        return data["choices"][0]["message"]["content"] if "choices" in data else "Error"

# --- GESTION DES HANDLERS ---
async def start(update, context):
    await update.message.reply_text("Bot actif ! Je connais l'heure exacte et je garde tout en mémoire.")

async def handle_text(update, context):
    user_id = update.effective_user.id
    text = update.message.text
    save_message(user_id, update.effective_chat.id, update.message.message_id, "text", text)
    save_memory(user_id, "user", text)
    
    response = await ask_groq(user_id, text)
    save_memory(user_id, "assistant", response)
    await update.message.reply_text(response)

# --- SERVEUR SANTÉ (Render) ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

# --- MAIN ---
def main():
    token = os.environ["BOT_TOKEN"]
    threading.Thread(target=run_web_server, daemon=True).start()
    
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    scheduler.start()
    app.run_polling()

if __name__ == "__main__":
    main()
    
