import os
import telebot
import requests
from telebot import types
import random
from flask import Flask
from threading import Thread
from datetime import datetime

# --- FLASK KEEP-ALIVE ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive and running."

def run_flask():
    app.run(host='0.0.0.0', port=8080)

# --- CONFIGURATION ---
API_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
bot = telebot.TeleBot(API_TOKEN)
USER_DB = "authorized_users.txt"

# --- STATS & DATA ---
START_TIME = datetime.now()
stats = {"searches": 0, "lyrics_delivered": 0}

RANDOM_QUERIES = ["bohemian rhapsody", "blinding lights", "hotel california", "shape of you"]
BTN_STATS, BTN_RANDOM = "📊 Stats", "🎲 Random"
MENU_BUTTONS = {BTN_STATS, BTN_RANDOM}

# --- UTILITIES ---
def register_user(user_id):
    if not is_user_authorized(user_id):
        with open(USER_DB, "a") as f:
            f.write(f"{user_id}\n")

def is_user_authorized(user_id):
    # FIX: return False (not True) when file is missing so registration works correctly
    if not os.path.exists(USER_DB):
        return False
    with open(USER_DB, "r") as f:
        return str(user_id) in f.read().splitlines()

# --- LOGIC ---
def do_search(message, query):
    stats['searches'] += 1
    status = bot.reply_to(message, f"🔍 Searching for *{query}*...", parse_mode='Markdown')
    try:
        res = requests.get(f"https://lrclib.net/api/search?q={query}")
        results = res.json()
        if not results:
            bot.edit_message_text("No results found.", message.chat.id, status.message_id)
            return
        markup = types.InlineKeyboardMarkup()
        for track in results[:6]:
            markup.add(types.InlineKeyboardButton(
                text=f"{track['artistName']} - {track['trackName']}",
                callback_data=f"sel_{track['id']}"
            ))
        bot.edit_message_text(
            f"Results for *{query}*:", message.chat.id, status.message_id,
            reply_markup=markup, parse_mode='Markdown'
        )
    except Exception as e:
        print(f"Search error: {e}")
        bot.edit_message_text("Connection error.", message.chat.id, status.message_id)

# --- HANDLERS ---
@bot.message_handler(commands=['start'])
def start(message):
    register_user(message.from_user.id)
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(BTN_STATS, BTN_RANDOM)
    bot.send_message(message.chat.id, "✅ *Ready.* Type a song name!", reply_markup=markup, parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text in MENU_BUTTONS)
def menu_handler(message):
    if not is_user_authorized(message.from_user.id):
        bot.reply_to(message, "⛔ Unauthorized. Send /start first.")
        return
    if message.text == BTN_RANDOM:
        do_search(message, random.choice(RANDOM_QUERIES))
    elif message.text == BTN_STATS:
        bot.reply_to(message, f"📊 Searches: {stats['searches']}\n🎵 Lyrics: {stats['lyrics_delivered']}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('sel_'))
def show_options(call):
    if not is_user_authorized(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Unauthorized.")
        return
    bot.answer_callback_query(call.id)
    track_id = call.data.split('_')[1]
    try:
        res = requests.get(f"https://lrclib.net/api/get/{track_id}").json()
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Get Lyrics", callback_data=f"pt_{track_id}"))
        bot.edit_message_text(
            f"🎵 {res['artistName']} - {res['trackName']}",
            call.message.chat.id, call.message.message_id,
            reply_markup=markup
        )
    except Exception as e:
        print(f"show_options error: {e}")
        bot.answer_callback_query(call.id, "Error fetching track info.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('pt_'))
def deliver_lyrics(call):
    if not is_user_authorized(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Unauthorized.")
        return
    bot.answer_callback_query(call.id)
    stats['lyrics_delivered'] += 1
    track_id = call.data.split('_')[1]
    try:
        res = requests.get(f"https://lrclib.net/api/get/{track_id}").json()
        lyrics = res.get('plainLyrics') or "No lyrics found."
        bot.send_message(call.message.chat.id, lyrics[:4000])
    except Exception as e:
        print(f"deliver_lyrics error: {e}")
        bot.send_message(call.message.chat.id, "Error fetching lyrics.")

@bot.message_handler(func=lambda m: m.chat.type == 'private')
def dm_search(message):
    if not is_user_authorized(message.from_user.id):
        bot.reply_to(message, "⛔ Unauthorized. Send /start first.")
        return
    do_search(message, message.text)

# --- STARTUP ---
if __name__ == "__main__":
    # FIX: was split across two lines as "ru\nn_flask" causing a SyntaxError
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()
    print("Bot is starting...")
    bot.infinity_polling()
