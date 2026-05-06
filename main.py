import os
import telebot
import requests
from telebot import types
import io
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
    # Render requires the app to run on 0.0.0.0
    app.run(host='0.0.0.0', port=8080)

# --- CONFIGURATION ---
# IMPORTANT: Ensure your Render Environment Variable is named TELEGRAM_BOT_TOKEN
API_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))
bot = telebot.TeleBot(API_TOKEN)
USER_DB = "authorized_users.txt"

# --- STATS & DATA ---
START_TIME = datetime.now()
stats = {"searches": 0, "lyrics_delivered": 0, "random_used": 0}
search_counts = {}

RANDOM_QUERIES = [
    "bohemian rhapsody", "blinding lights", "hotel california", "shape of you",
    "rolling in the deep", "smells like teen spirit", "thriller", "lose yourself",
    "someone like you", "stay with me", "bad guy", "levitating", "drivers license",
    "montero", "watermelon sugar", "good 4 u", "shivers", "easy on me",
    "as it was", "unholy", "flowers", "cruel summer", "vampire", "golden hour"
]

TRENDING_POOL = [
    "espresso sabrina carpenter", "please please please sabrina carpenter",
    "apt rose bruno mars", "die with a smile lady gaga bruno mars",
    "birds of a feather billie eilish", "lunch billie eilish",
    "too sweet hozier", "fortnight taylor swift",
    "not like us kendrick lamar", "million dollar baby tommy richman",
    "beautiful things benson boone", "we can't be friends ariana grande",
    "good luck luck dua lipa", "illusion dua lipa",
    "lose control teddy swims", "i had some help morgan wallen",
    "texas hold em beyonce", "salvation chris brown",
    "euphoria kendrick lamar", "squabble up kendrick lamar",
    "timeless the weeknd playboi carti", "sinner pray for me the weeknd",
    "luther kendrick lamar sza", "tv off kendrick lamar"
]

BTN_HELP      = "❓ Help"
BTN_STATS     = "📊 Stats"
BTN_TRENDING  = "🔥 Trending"
BTN_TOP       = "🏆 Top"
BTN_RANDOM    = "🎲 Random"
BTN_ADD_GROUP = "➕ Add to Group"
BTN_LISTUSERS = "👥 List Users"
BTN_BROADCAST = "📢 Broadcast"
MENU_BUTTONS  = {BTN_HELP, BTN_STATS, BTN_TRENDING, BTN_TOP, BTN_RANDOM, BTN_ADD_GROUP, BTN_LISTUSERS, BTN_BROADCAST}

# --- DATABASE UTILITIES ---
def register_user(user_id):
    if not is_user_authorized(user_id):
        with open(USER_DB, "a") as f:
            f.write(f"{user_id}\n")

def is_user_authorized(user_id):
    if not os.path.exists(USER_DB):
        return True # Fallback so the bot doesn't lock everyone out if file is missing
    with open(USER_DB, "r") as f:
        authorized_users = f.read().splitlines()
    return str(user_id) in authorized_users

# --- HELPERS ---
def format_duration(seconds):
    if not seconds: return "N/A"
    return f"{seconds // 60}m {seconds % 60}s"

def get_requester(message):
    if message.chat.type in ['group', 'supergroup']:
        user = message.from_user
        name = user.first_name + (f" {user.last_name}" if user.last_name else "")
        return f"👤 Requested by: *{name}*\n"
    return ""

def get_menu_markup(user_id=None):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(types.KeyboardButton(BTN_HELP), types.KeyboardButton(BTN_STATS))
    markup.row(types.KeyboardButton(BTN_TRENDING), types.KeyboardButton(BTN_TOP))
    markup.row(types.KeyboardButton(BTN_RANDOM))
    markup.row(types.KeyboardButton(BTN_ADD_GROUP))
    if user_id and user_id == ADMIN_ID:
        markup.row(types.KeyboardButton(BTN_LISTUSERS), types.KeyboardButton(BTN_BROADCAST))
    return markup

def do_search(message, query):
    stats['searches'] += 1
    requester = get_requester(message)
    search_counts[query.lower()] = search_counts.get(query.lower(), 0) + 1
    status = bot.reply_to(message, f"🔍 Searching for *{query}*...", parse_mode='Markdown')
    try:
        res = requests.get(f"https://lrclib.net/api/search?q={query}")
        results = res.json()
        if not results:
            bot.edit_message_text("No results found.", message.chat.id, status.message_id)
            return
        markup = types.InlineKeyboardMarkup()
        for track in results[:6]:
            btn_text = f"{track['artistName']} — {track['trackName']}"
            markup.add(types.InlineKeyboardButton(text=btn_text, callback_data=f"sel_{track['id']}"))
        prompt = f"{requester}Select a version:" if requester else f"Results for *{query}*:"
        bot.edit_message_text(prompt, message.chat.id, status.message_id, reply_markup=markup, parse_mode='Markdown')
    except:
        bot.edit_message_text("Connection error.", message.chat.id, status.message_id)

# --- COMMAND HANDLERS ---
@bot.message_handler(commands=['start'])
def start(message):
    register_user(message.from_user.id)
    bot.send_message(message.chat.id, "✅ *Registration complete.*", reply_markup=get_menu_markup(message.from_user.id), parse_mode='Markdown')

@bot.message_handler(commands=['stats'])
def send_stats(message):
    uptime = datetime.now() - START_TIME
    text = f"📊 *Bot Statistics*\n\n🔍 Searches: {stats['searches']}\n🎵 Lyrics: {stats['lyrics_delivered']}\n🎲 Random: {stats['random_used']}"
    bot.reply_to(message, text, parse_mode='Markdown')

@bot.message_handler(commands=['random'])
def random_song(message):
    stats['random_used'] += 1
    query = random.choice(RANDOM_QUERIES)
    status = bot.reply_to(message, f"🎲 Fetching: *{query}*...", parse_mode='Markdown')
    try:
        res = requests.get(f"https://lrclib.net/api/search?q={query}")
        results = res.json()
        pick = random.choice(results[:6])
        track_id = pick['id']
        res2 = requests.get(f"https://lrclib.net/api/get/{track_id}")
        t = res2.json()
        info = f"🎲 *Random Pick*\n\n🎵 {t['artistName']} — {t['trackName']}"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Synced Text", callback_data=f"st_{track_id}"), 
                   types.InlineKeyboardButton("Plain Text", callback_data=f"pt_{track_id}"))
        markup.add(types.InlineKeyboardButton("🎲 Another Random", callback_data="random"))
        bot.edit_message_text(info, message.chat.id, status.message_id, reply_markup=markup, parse_mode='Markdown')
    except:
        bot.edit_message_text("Error fetching random song.", message.chat.id, status.message_id)

# --- CALLBACK HANDLERS (THE FIXES) ---

@bot.callback_query_handler(func=lambda call: call.data == "random")
def random_callback(call):
    bot.answer_callback_query(call.id) # FIX: Stops the spinner
    random_song(call.message)

@bot.callback_query_handler(func=lambda call: call.data.startswith('sel_'))
def show_options(call):
    bot.answer_callback_query(call.id) # FIX: Stops the spinner
    track_id = call.data.split('_')[1]
    res = requests.get(f"https://lrclib.net/api/get/{track_id}")
    t = res.json()
    info = f"🎵 {t['artistName']} — {t['trackName']}\n💿 Album: {t.get('albumName', 'N/A')}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Synced Text", callback_data=f"st_{track_id}"),
               types.InlineKeyboardButton("Plain Text", callback_data=f"pt_{track_id}"))
    bot.edit_message_text(info, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith(('sf_', 'st_', 'pf_', 'pt_')))
def deliver_lyrics(call):
    bot.answer_callback_query(call.id) # FIX: Stops the spinner
    stats['lyrics_delivered'] += 1
    prefix, track_id = call.data.split('_')
    res = requests.get(f"https://lrclib.net/api/get/{track_id}")
    t = res.json()
    lyrics = t.get('syncedLyrics') if 's' in prefix else t.get('plainLyrics')
    if lyrics:
        bot.send_message(call.message.chat.id, lyrics[:4000])
    else:
        bot.send_message(call.message.chat.id, "Lyrics not available for this version.")

# --- DM TEXT HANDLER ---
@bot.message_handler(func=lambda m: m.chat.type == 'private' and m.text not in MENU_BUTTONS)
def dm_search(message):
    do_search(message, message.text)

# --- STARTUP LOGIC (FIX FOR STATUS 1) ---
if __name__ == "__main__":
    # Start the web server thread
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    print("Bot is successfully starting...")
    # Start the bot
    bot.infinity_polling()
    "as it was", "unholy", "flowers", "cruel summer", "vampire", "golden hour"
]

TRENDING_POOL = [
    "espresso sabrina carpenter", "please please please sabrina carpenter",
    "apt rose bruno mars", "die with a smile lady gaga bruno mars",
    "birds of a feather billie eilish", "lunch billie eilish",
    "too sweet hozier", "fortnight taylor swift",
    "not like us kendrick lamar", "million dollar baby tommy richman",
    "beautiful things benson boone", "we can't be friends ariana grande",
    "good luck luck dua lipa", "illusion dua lipa",
    "lose control teddy swims", "i had some help morgan wallen",
    "texas hold em beyonce", "salvation chris brown",
    "euphoria kendrick lamar", "squabble up kendrick lamar",
    "timeless the weeknd playboi carti", "sinner pray for me the weeknd",
    "luther kendrick lamar sza", "tv off kendrick lamar"
]

# --- MENU BUTTON LABELS ---
BTN_HELP      = "❓ Help"
BTN_STATS     = "📊 Stats"
BTN_TRENDING  = "🔥 Trending"
BTN_TOP       = "🏆 Top"
BTN_RANDOM    = "🎲 Random"
BTN_ADD_GROUP = "➕ Add to Group"
MENU_BUTTONS  = {BTN_HELP, BTN_STATS, BTN_TRENDING, BTN_TOP, BTN_RANDOM, BTN_ADD_GROUP,
                 "👥 List Users", "📢 Broadcast"}

# --- DATABASE UTILITIES ---
def register_user(user_id):
    if not is_user_authorized(user_id):
        with open(USER_DB, "a") as f:
            f.write(f"{user_id}\n")

def is_user_authorized(user_id):
    if not os.path.exists(USER_DB):
        return False
    with open(USER_DB, "r") as f:
        authorized_users = f.read().splitlines()
    return str(user_id) in authorized_users

# --- HELPERS ---
def format_duration(seconds):
    if not seconds:
        return "N/A"
    return f"{seconds // 60}m {seconds % 60}s"

def get_requester(message):
    if message.chat.type in ['group', 'supergroup']:
        user = message.from_user
        name = user.first_name
        if user.last_name:
            name += f" {user.last_name}"
        return f"👤 Requested by: *{name}*\n"
    return ""

BTN_LISTUSERS = "👥 List Users"
BTN_BROADCAST = "📢 Broadcast"

def get_menu_markup(user_id=None):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    markup.row(types.KeyboardButton(BTN_HELP), types.KeyboardButton(BTN_STATS))
    markup.row(types.KeyboardButton(BTN_TRENDING), types.KeyboardButton(BTN_TOP))
    markup.row(types.KeyboardButton(BTN_RANDOM))
    markup.row(types.KeyboardButton(BTN_ADD_GROUP))
    if user_id and user_id == ADMIN_ID:
        markup.row(types.KeyboardButton(BTN_LISTUSERS), types.KeyboardButton(BTN_BROADCAST))
    return markup

def do_search(message, query):
    """Shared search logic for both DM free-text and /lyrics command."""
    stats['searches'] += 1
    requester = get_requester(message)
    search_counts[query.lower()] = search_counts.get(query.lower(), 0) + 1
    status = bot.reply_to(message, f"🔍 Searching for *{query}*...", parse_mode='Markdown')
    try:
        res = requests.get(f"https://lrclib.net/api/search?q={query}")
        results = res.json()
        if not results:
            bot.edit_message_text("No results found.", message.chat.id, status.message_id)
            return
        markup = types.InlineKeyboardMarkup()
        for track in results[:6]:
            btn_text = f"{track['artistName']} — {track['trackName']}"
            markup.add(types.InlineKeyboardButton(text=btn_text, callback_data=f"sel_{track['id']}"))
        prompt = f"{requester}Select a version:" if requester else f"Results for *{query}*:"
        bot.edit_message_text(prompt, message.chat.id, status.message_id,
                              reply_markup=markup, parse_mode='Markdown')
    except:
        bot.edit_message_text("Connection error.", message.chat.id, status.message_id)

# --- COMMAND HANDLERS ---

@bot.message_handler(commands=['start'])
def start(message):
    if message.chat.type == 'private':
        register_user(message.from_user.id)
        bot.send_message(
            message.chat.id,
            "✅ *Registration complete.*\n\nYou can now use the lyrics service here or in groups.\n\n"
            "_In DM: just type any song name to search._\n"
            "_In groups: use_ `/lyrics <song>`",
            reply_markup=get_menu_markup(message.from_user.id),
            parse_mode='Markdown'
        )
    else:
        bot.reply_to(message, "Please use this command in a Direct Message to register.")

@bot.message_handler(commands=['help'])
def help_cmd(message):
    bot.send_message(
        message.chat.id,
        "🎵 *Lyrics Bot — Command Guide*\n\n"
        "🔹 *In DM:* Just type any song name to search\n"
        "🔹 *In Groups:* Use `/lyrics <song>`\n\n"
        "🎲 /random — Get a random song\n"
        "🔥 /trending — See what's trending now\n"
        "🏆 /top — Most searched songs on this bot\n"
        "📊 /stats — Usage statistics\n\n"
        "_Example (group):_ /lyrics blinding lights",
        reply_markup=get_menu_markup(message.from_user.id),
        parse_mode='Markdown'
    )

@bot.message_handler(commands=['stats'])
def send_stats(message):
    uptime = datetime.now() - START_TIME
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"
    text = (
        f"📊 *Bot Statistics*\n\n"
        f"🔍 Searches performed: {stats['searches']}\n"
        f"🎵 Lyrics delivered: {stats['lyrics_delivered']}\n"
        f"🎲 Random used: {stats['random_used']}\n"
        f"⏱ Uptime: {uptime_str}"
    )
    bot.reply_to(message, text, parse_mode='Markdown')

@bot.message_handler(commands=['top'])
def send_top(message):
    if not search_counts:
        bot.reply_to(message, "📭 No searches recorded yet.")
        return
    sorted_songs = sorted(search_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = [f"{i+1}. {query} — {count} search{'es' if count != 1 else ''}"
             for i, (query, count) in enumerate(sorted_songs)]
    text = "🏆 *Top Searched Songs*\n\n" + "\n".join(lines)
    bot.reply_to(message, text, parse_mode='Markdown')

@bot.message_handler(commands=['trending'])
def send_trending(message):
    picks = random.sample(TRENDING_POOL, min(8, len(TRENDING_POOL)))
    markup = types.InlineKeyboardMarkup()
    for pick in picks:
        markup.add(types.InlineKeyboardButton(
            text=f"🔥 {pick.title()}",
            callback_data=f"trend_{pick}"
        ))
    bot.reply_to(message, "🔥 *Trending Right Now*\nTap a song to search its lyrics:",
                 reply_markup=markup, parse_mode='Markdown')

@bot.message_handler(commands=['random'])
def random_song(message):
    stats['random_used'] += 1
    requester = get_requester(message)
    query = random.choice(RANDOM_QUERIES)
    status = bot.reply_to(message, f"🎲 Fetching a random track: *{query}*...", parse_mode='Markdown')
    try:
        res = requests.get(f"https://lrclib.net/api/search?q={query}")
        results = res.json()
        if not results:
            bot.edit_message_text("No results found. Try again.", message.chat.id, status.message_id)
            return
        pick = random.choice(results[:6])
        track_id = pick['id']
        res2 = requests.get(f"https://lrclib.net/api/get/{track_id}")
        t = res2.json()
        info = (
            f"{requester}🎲 *Random Pick*\n\n"
            f"🎵 {t['artistName']} — {t['trackName']}\n"
            f"💿 Album: {t.get('albumName', 'N/A')}\n"
            f"⏱ Duration: {format_duration(t.get('duration'))}"
        )
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("Synced → File", callback_data=f"sf_{track_id}"),
            types.InlineKeyboardButton("Synced Text", callback_data=f"st_{track_id}"),
            types.InlineKeyboardButton("Plain → File", callback_data=f"pf_{track_id}"),
            types.InlineKeyboardButton("Plain Text", callback_data=f"pt_{track_id}")
        )
        markup.add(types.InlineKeyboardButton("🎲 Another Random", callback_data="random"))
        bot.edit_message_text(info, message.chat.id, status.message_id,
                              reply_markup=markup, parse_mode='Markdown')
    except:
        bot.edit_message_text("Connection error. Try again.", message.chat.id, status.message_id)

@bot.message_handler(commands=['lyrics', 'search'])
def lyrics_cmd(message):
    user_id = message.from_user.id
    if not is_user_authorized(user_id):
        bot_username = bot.get_me().username
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Start Bot in DM", url=f"https://t.me/{bot_username}?start=true"))
        bot.reply_to(message,
                     "⛔ *Access Denied.*\nPlease start the bot in a Direct Message first.",
                     reply_markup=markup, parse_mode='Markdown')
        return
    query_parts = message.text.split(maxsplit=1)
    if len(query_parts) < 2 or not query_parts[1].strip():
        bot.reply_to(message, "Usage: `/lyrics <song name>`", parse_mode='Markdown')
        return
    do_search(message, query_parts[1].strip())

# Admin commands
@bot.message_handler(commands=['users'])
def users_cmd(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Admin only.")
        return
    if not os.path.exists(USER_DB):
        bot.reply_to(message, "👥 Registered users: 0")
        return
    with open(USER_DB, "r") as f:
        count = len([uid for uid in f.read().splitlines() if uid.strip()])
    bot.reply_to(message, f"👥 Registered users: *{count}*", parse_mode='Markdown')

@bot.message_handler(commands=['listusers'])
def list_users(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Admin only.")
        return
    if not os.path.exists(USER_DB):
        bot.reply_to(message, "📭 No registered users yet.")
        return
    with open(USER_DB, "r") as f:
        users = [uid.strip() for uid in f.read().splitlines() if uid.strip()]
    if not users:
        bot.reply_to(message, "📭 No registered users yet.")
        return
    lines = "\n".join([f"{i+1}. `{uid}`" for i, uid in enumerate(users)])
    bot.reply_to(message, f"👥 *Registered Users ({len(users)})*\n\n{lines}", parse_mode='Markdown')

@bot.message_handler(commands=['remove'])
def remove_user(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Admin only.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "Usage: `/remove <user_id>`", parse_mode='Markdown')
        return
    target_id = parts[1].strip()
    if not os.path.exists(USER_DB):
        bot.reply_to(message, "No registered users found.")
        return
    with open(USER_DB, "r") as f:
        users = [uid.strip() for uid in f.read().splitlines() if uid.strip()]
    if target_id not in users:
        bot.reply_to(message, f"❌ User `{target_id}` is not registered.", parse_mode='Markdown')
        return
    users.remove(target_id)
    with open(USER_DB, "w") as f:
        f.write("\n".join(users) + ("\n" if users else ""))
    bot.reply_to(message, f"✅ User `{target_id}` has been removed.", parse_mode='Markdown')

@bot.message_handler(commands=['broadcast'])
def broadcast(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Admin only.")
        return
    if message.chat.type != 'private':
        bot.reply_to(message, "⚠️ Use this command in DM.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "Usage: `/broadcast <message>`", parse_mode='Markdown')
        return
    text = parts[1].strip()
    if not os.path.exists(USER_DB):
        bot.reply_to(message, "No registered users yet.")
        return
    with open(USER_DB, "r") as f:
        user_ids = [uid.strip() for uid in f.read().splitlines() if uid.strip()]
    sent, failed = 0, 0
    for uid in user_ids:
        try:
            bot.send_message(int(uid), f"📢 *Announcement*\n\n{text}", parse_mode='Markdown')
            sent += 1
        except:
            failed += 1
    bot.reply_to(message, f"✅ Broadcast complete.\n\n📤 Sent: {sent}\n❌ Failed: {failed}")

# --- MENU BUTTON TEXT HANDLER (must be before catch-all) ---
@bot.message_handler(func=lambda m: m.text in MENU_BUTTONS)
def handle_menu_buttons(message):
    t = message.text
    if t == BTN_HELP:
        help_cmd(message)
    elif t == BTN_STATS:
        send_stats(message)
    elif t == BTN_TRENDING:
        send_trending(message)
    elif t == BTN_TOP:
        send_top(message)
    elif t == BTN_RANDOM:
        random_song(message)
    elif t == BTN_ADD_GROUP:
        bot_username = bot.get_me().username
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            "➕ Add me to a Group",
            url=f"https://t.me/{bot_username}?startgroup=true"
        ))
        bot.reply_to(message, "Tap the button below to add me to a group:", reply_markup=markup)
    elif t == BTN_LISTUSERS:
        if message.from_user.id == ADMIN_ID:
            list_users(message)
    elif t == BTN_BROADCAST:
        if message.from_user.id == ADMIN_ID:
            bot.reply_to(message, "Usage: `/broadcast <message>`", parse_mode='Markdown')

# --- DM FREE-TEXT SEARCH (private chats only, not a command or button) ---
@bot.message_handler(func=lambda m: m.chat.type == 'private')
def dm_search(message):
    user_id = message.from_user.id
    if not is_user_authorized(user_id):
        bot_username = bot.get_me().username
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Start Bot in DM", url=f"https://t.me/{bot_username}?start=true"))
        bot.reply_to(message,
                     "⛔ *Access Denied.*\nPlease send /start first to register.",
                     reply_markup=markup, parse_mode='Markdown')
        return
    do_search(message, message.text)

# --- CALLBACK HANDLERS ---

@bot.callback_query_handler(func=lambda call: call.data.startswith('trend_'))
def handle_trending_pick(call):
    query = call.data[len('trend_'):]
    stats['searches'] += 1
    search_counts[query.lower()] = search_counts.get(query.lower(), 0) + 1
    try:
        res = requests.get(f"https://lrclib.net/api/search?q={query}")
        results = res.json()
        if not results:
            bot.answer_callback_query(call.id, "No results found.")
            return
        markup = types.InlineKeyboardMarkup()
        for track in results[:6]:
            btn_text = f"{track['artistName']} — {track['trackName']}"
            markup.add(types.InlineKeyboardButton(text=btn_text, callback_data=f"sel_{track['id']}"))
        bot.send_message(call.message.chat.id, f"🔥 Results for *{query.title()}*:",
                         reply_markup=markup, parse_mode='Markdown')
    except:
        bot.answer_callback_query(call.id, "Connection error.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('sel_'))
def show_options(call):
    track_id = call.data.split('_')[1]
    res = requests.get(f"https://lrclib.net/api/get/{track_id}")
    t = res.json()
    info = (f"🎵 {t['artistName']} — {t['trackName']}\n"
            f"💿 Album: {t.get('albumName', 'N/A')}\n"
            f"⏱ Duration: {format_duration(t.get('duration'))}")
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("Synced → File", callback_data=f"sf_{track_id}"),
        types.InlineKeyboardButton("Synced Text", callback_data=f"st_{track_id}"),
        types.InlineKeyboardButton("Plain → File", callback_data=f"pf_{track_id}"),
        types.InlineKeyboardButton("Plain Text", callback_data=f"pt_{track_id}")
    )
    markup.add(types.InlineKeyboardButton("Back", callback_data="back"))
    bot.edit_message_text(info, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith(('sf_', 'st_', 'pf_', 'pt_')))
def deliver_lyrics(call):
    stats['lyrics_delivered'] += 1
    cmd, track_id = call.data.split('_')
    res = requests.get(f"https://lrclib.net/api/get/{track_id}")
    t = res.json()
    name = f"{t['artistName']} - {t['trackName']}"
    if cmd == "sf":
        data = t.get('syncedLyrics')
        if data:
            bio = io.BytesIO(data.encode('utf-8'))
            bio.name = f"{name}.lrc"
            bot.send_document(call.message.chat.id, bio)
        else:
            bot.answer_callback_query(call.id, "Not available.")
    elif cmd == "st":
        data = t.get('syncedLyrics')
        if data:
            bot.send_message(call.message.chat.id, f"`{data[:4000]}`", parse_mode='MarkdownV2')
        else:
            bot.answer_callback_query(call.id, "Not available.")
    elif cmd == "pf":
        data = t.get('plainLyrics')
        if data:
            bio = io.BytesIO(data.encode('utf-8'))
            bio.name = f"{name}.txt"
            bot.send_document(call.message.chat.id, bio)
        else:
            bot.answer_callback_query(call.id, "Not available.")
    elif cmd == "pt":
        data = t.get('plainLyrics')
        if data:
            bot.send_message(call.message.chat.id, f"{name}\n\n{data[:4000]}")
        else:
            bot.answer_callback_query(call.id, "Not available.")

@bot.callback_query_handler(func=lambda call: call.data == "random")
def random_callback(call):
    stats['random_used'] += 1
    query = random.choice(RANDOM_QUERIES)
    try:
        res = requests.get(f"https://lrclib.net/api/search?q={query}")
        results = res.json()
        if not results:
            bot.answer_callback_query(call.id, "Nothing found, try again.")
            return
        pick = random.choice(results[:6])
        track_id = pick['id']
        res2 = requests.get(f"https://lrclib.net/api/get/{track_id}")
        t = res2.json()
        info = (f"🎲 *Random Pick*\n\n"
                f"🎵 {t['artistName']} — {t['trackName']}\n"
                f"💿 Album: {t.get('albumName', 'N/A')}\n"
                f"⏱ Duration: {format_duration(t.get('duration'))}")
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("Synced → File", callback_data=f"sf_{track_id}"),
            types.InlineKeyboardButton("Synced Text", callback_data=f"st_{track_id}"),
            types.InlineKeyboardButton("Plain → File", callback_data=f"pf_{track_id}"),
            types.InlineKeyboardButton("Plain Text", callback_data=f"pt_{track_id}")
        )
        markup.add(types.InlineKeyboardButton("🎲 Another Random", callback_data="random"))
        bot.edit_message_text(info, call.message.chat.id, call.message.message_id,
                              reply_markup=markup, parse_mode='Markdown')
    except:
        bot.answer_callback_query(call.id, "Connection error. Try again.")

@bot.callback_query_handler(func=lambda call: call.data == "back")
def back(call):
    bot.edit_message_text("Type a song name to search, or use the menu below.",
                          call.message.chat.id, call.message.message_id)

# --- START ---
bot_thread = Thread(target=start_bot)
bot_thread.daemon = True
bot_thread.start()

port = int(os.environ.get('PORT', 8080))
app.run(host='0.0.0.0', port=port)
