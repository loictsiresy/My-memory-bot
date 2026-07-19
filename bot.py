import os
import re
import pytz
import httpx
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- CONFIGURATION ---
# Assure-toi que BOT_TOKEN et GROQ_API_KEY sont définis dans tes variables d'environnement
TOKEN = os.environ["BOT_TOKEN"]
GROQ_KEY = os.environ["GROQ_API_KEY"]
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# --- SYSTEM PROMPT ---
SYSTEM_PROMPT = """Tu es un assistant personnel. 
Ton rôle est de répondre aux demandes et de gérer des rappels.
Si l'utilisateur demande un rappel, tu DOIS terminer ta réponse par ce format strict :
[REMINDER:DD-MM-YYYY HH:MM|Description du rappel]
Aujourd'hui est le {date_str}. Utilise cette date pour tes calculs."""

# --- INITIALISATION ---
scheduler = AsyncIOScheduler()
scheduler.start()

# --- FONCTION DE RAPPEL ---
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Fonction appelée par le JobQueue pour envoyer le message de rappel."""
    job = context.job
    await context.bot.send_message(chat_id=job.chat_id, text=f"🔔 RAPPEL : {job.data}")

# --- ANALYSE ET PLANIFICATION ---
def parse_and_schedule(chat_id, ai_response, app):
    """Analyse la réponse de l'IA pour extraire et programmer un rappel."""
    match = re.search(r'\[REMINDER:(\d{2}-\d{2}-\d{4} \d{2}:\d{2})\|([^\]]+)\]', ai_response)
    if match:
        time_str, description = match.group(1), match.group(2)
        try:
            reminder_time = datetime.strptime(time_str, "%d-%m-%Y %H:%M").replace(tzinfo=pytz.UTC)
            # Utilisation de la JobQueue intégrée à l'application
            app.job_queue.run_once(send_reminder, when=reminder_time, chat_id=chat_id, data=description)
            return True
        except ValueError:
            return False
    return False

# --- LOGIQUE IA (GROQ) ---
async def ask_groq(user_message):
    tz = pytz.timezone('Africa/Nairobi')
    date_str = datetime.now(tz).strftime("%d-%m-%Y")
    
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(date_str=date_str)},
        {"role": "user", "content": user_message}
    ]
    
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            GROQ_URL, 
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.1-8b-instant", "messages": messages, "temperature": 0.3}
        )
        return response.json()["choices"][0]["message"]["content"]

# --- HANDLERS ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.caption or update.message.text
    response = await ask_groq(text)
    
    # Tentative de programmation
    if parse_and_schedule(update.effective_chat.id, response, context.application):
        await update.message.reply_text(f"{response}\n\n✅ Rappel programmé.")
    else:
        await update.message.reply_text(response)

async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jobs = context.job_queue.jobs()
    if not jobs:
        await update.message.reply_text("Aucun rappel programmé.")
        return
    text = "📋 Rappels en cours :\n" + "\n".join([f"- {j.data}" for j in jobs])
    await update.message.reply_text(text)

# --- MAIN ---
def main():
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("list", list_reminders))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))
    
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
    
