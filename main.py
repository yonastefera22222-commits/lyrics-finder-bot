import os
import telebot
import requests
from telebot import types
import io
import random
from flask import Flask
from threading import Thread
from datetime import datetime

# --- OWNER ID (set your Telegram user ID here) ---
OWNER_ID = 951232574  # <<<<<<------- OWNER ID SET HERE

# --- Group/Private Chat ID Storage for Broadcasts ---
GROUP_CHAT_DB = "group_chats.txt"
PRIVATE_CHAT_DB = "private_chats.txt"

def save_chat(chat):
    if chat.type in ("group", "supergroup"):
        db_file = GROUP_CHAT_DB
    elif chat.type == "private":
        db_file = PRIVATE_CHAT_DB
    else:
        return
    chat_id = str(chat.id)
    if not os.path.exists(db_file):
        with open(db_file, "w") as f:
            f.write(chat_id + "\n")
        return
    with open(db_file, "r+") as f:
        ids = set(line.strip() for line in f)
        if chat_id not in ids:
            f.write(chat_id + "\n")

def get_all_target_chats():
    ids = set()
    if os.path.exists(PRIVATE_CHAT_DB):
        with open(PRIVATE_CHAT_DB) as f:
            ids.update(int(line.strip()) for line in f if line.strip())
    if os.path.exists(GROUP_CHAT_DB):
        with open(GROUP_CHAT_DB) as f:
            ids.update(int(line.strip()) for line in f if line.strip())
    return list(ids)

# --- FLASK KEEP-ALIVE ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive and running."

def start_bot():
    bot.infinity_polling()

# --- CONFIGURATION ---
API_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
if not API_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN environment variable")

bot = telebot.TeleBot(API_TOKEN)

# --- INIT ADMIN DB AND MIGRATE TEXT ADMINS ---
import admin_db
admin_db.setup_db()
if os.path.exists("authorized_users.txt"):
    with open("authorized_users.txt") as f:
        for uid in f.read().splitlines():
            if uid.strip().isdigit():
                admin_db.add_admin(uid.strip())
    # Optionally remove file after migration
    # os.remove("authorized_users.txt")

# --- STATS ---
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

def escape_markdown_v2(text):
    chars = r"\_*[]()~`>#+-=|{}.!<>"
    return "".join("\\" + c if c in chars else c for c in text)

BTN_HELP      = "❓ Help"
BTN_STATS     = "📊 Stats"
BTN_TRENDING  = "🔥 Trending"
BTN_TOP       = "🏆 Top"
BTN_RANDOM    = "🎲 Random"
BTN_ADD_GROUP = "➕ Add to Group"
MENU_BUTTONS  = {BTN_HELP, BTN_STATS, BTN_TRENDING, BTN_TOP, BTN_RANDOM, BTN_ADD_GROUP,
                 "👥 List Admins", "📢 Broadcast", "➕ Add Admin", "➖ Remove Admin"}

BTN_LISTADMINS = "👥 List Admins"
BTN_BROADCAST = "📢 Broadcast"
BTN_ADDADMIN = "➕ Add Admin"
BTN_REMOVEADMIN = "➖ Remove Admin"

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

def get_menu_markup(user_id=None):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    markup.row(types.KeyboardButton(BTN_HELP), types.KeyboardButton(BTN_STATS))
    markup.row(types.KeyboardButton(BTN_TRENDING), types.KeyboardButton(BTN_TOP))
    markup.row(types.KeyboardButton(BTN_RANDOM))
    markup.row(types.KeyboardButton(BTN_ADD_GROUP))
    if user_id and admin_db.is_admin(user_id):
        if user_id == OWNER_ID:
            markup.row(
                types.KeyboardButton(BTN_LISTADMINS),
                types.KeyboardButton(BTN_BROADCAST),
                types.KeyboardButton(BTN_ADDADMIN),
                types.KeyboardButton(BTN_REMOVEADMIN)
            )
        else:
            markup.row(
                types.KeyboardButton(BTN_LISTADMINS),
                types.KeyboardButton(BTN_BROADCAST)
            )
    return markup

def split_lines_for_telegram(text, max_len=4096):
    lines = text.splitlines(keepends=True)
    chunks = []
    current = ""
    for line in lines:
        if len(current) + len(line) > max_len and current:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks

def do_search(message, query):
    save_chat(message.chat)
    stats['searches'] += 1
    requester = get_requester(message)
    search_counts[query.lower()] = search_counts.get(query.lower(), 0) + 1
    status = bot.reply_to(message, f"🔍 Searching for *{query}*...", parse_mode='Markdown')
    try:
        res = requests.get(f"https://lrclib.net/api/search?q={query}")
        results = res.json()
        query_low = query.strip().lower()
        filtered = [track for track in results
                    if query_low in track['trackName'].lower() or query_low in track['artistName'].lower()]
        display_tracks = filtered if filtered else results[:6]

        if not display_tracks:
            bot.edit_message_text("No relevant results found.", message.chat.id, status.message_id)
            return
        markup = types.InlineKeyboardMarkup()
        for track in display_tracks[:6]:
            btn_text = f"{track['artistName']} — {track['trackName']}"
            markup.add(types.InlineKeyboardButton(text=btn_text, callback_data=f"sel_{track['id']}"))
        if filtered:
            prompt = f"{requester}Select a version:" if requester else f"Results for *{query}*:"
        else:
            prompt = f"{requester}No exact matches, showing close results for *{query}*:"
        bot.edit_message_text(prompt, message.chat.id, status.message_id,
                              reply_markup=markup, parse_mode='Markdown')
    except Exception:
        import traceback
        print(traceback.format_exc())
        try:
            bot.edit_message_text("Connection error.", message.chat.id, status.message_id)
        except Exception:
            pass

@bot.message_handler(commands=['start'])
def start(message):
    save_chat(message.chat)
    bot.send_message(
        message.chat.id,
        "✅ *Welcome!*\n\nYou can now use the lyrics service here or in groups.\n\n"
        "_In DM: just type any song name to search._\n"
        "_In groups: use_ `/lyrics `",
        reply_markup=get_menu_markup(message.from_user.id),
        parse_mode='Markdown'
    )

@bot.message_handler(commands=['help'])
def help_cmd(message):
    save_chat(message.chat)
    bot.send_message(
        message.chat.id,
        "🎵 *Lyrics Bot — Command Guide*\n\n"
        "🔹 *In DM:* Just type any song name to search\n"
        "🔹 *In Groups:* Use `/lyrics `\n\n"
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
    save_chat(message.chat)
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
    save_chat(message.chat)
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
    save_chat(message.chat)
    picks = random.sample(TRENDING_POOL, min(8, len(TRENDING_POOL)))
    markup = types.InlineKeyboardMarkup()
    for pick in picks:
        markup.add(types.InlineKeyboardButton(
            text=f"🔥 {pick.title()}",
            callback_data=f"trend_{pick}"
        ))
    bot.send_message(message.chat.id, "🔥 *Trending Right Now*\nTap a song to search its lyrics:",
                     reply_markup=markup, parse_mode='Markdown')

@bot.message_handler(commands=['random'])
def random_song(message):
    save_chat(message.chat)
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
    except Exception:
        import traceback
        print(traceback.format_exc())
        try:
            bot.edit_message_text("Connection error. Try again.", message.chat.id, status.message_id)
        except Exception:
            pass

@bot.message_handler(commands=['lyrics', 'search'])
def lyrics_cmd(message):
    save_chat(message.chat)
    user_id = message.from_user.id
    if not admin_db.is_admin(user_id):
        bot_username = bot.get_me().username
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Start Bot in DM", url=f"https://t.me/{bot_username}?start=true"))
        bot.reply_to(message,
                     "⛔ *Access Denied.*\nPlease ask an admin to add you first.",
                     reply_markup=markup, parse_mode='Markdown')
        return
    query_parts = message.text.split(maxsplit=1)
    if len(query_parts) < 2 or not query_parts[1].strip():
        bot.reply_to(message, "Usage: `/lyrics `", parse_mode='Markdown')
        return
    do_search(message, query_parts[1].strip())

@bot.message_handler(commands=['addadmin'])
def addadmin_cmd(message):
    save_chat(message.chat)
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "⛔ Owner only.")
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].strip().isdigit():
        bot.reply_to(message, "Usage: /addadmin <user_id>")
        return
    user_id = parts[1].strip()
    admin_db.add_admin(user_id)
    bot.reply_to(message, f"✅ User `{user_id}` is now an admin.", parse_mode="Markdown")

@bot.message_handler(commands=['removeadmin'])
def removeadmin_cmd(message):
    save_chat(message.chat)
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "⛔ Owner only.")
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].strip().isdigit():
        bot.reply_to(message, "Usage: /removeadmin <user_id>")
        return
    user_id = parts[1].strip()
    admin_db.remove_admin(user_id)
    bot.reply_to(message, f"✅ User `{user_id}` is no longer an admin.", parse_mode="Markdown")

@bot.message_handler(commands=['admins'])
def admins_cmd(message):
    save_chat(message.chat)
    if not admin_db.is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Admin only.")
        return
    admin_list = admin_db.get_all_admins()
    if not admin_list:
        bot.reply_to(message, "📭 No admins set yet.")
    else:
        lines = "\n".join([f"{i+1}. `{uid}`" for i, uid in enumerate(admin_list)])
        bot.reply_to(message, f"👥 *Admins List ({len(admin_list)})*\n\n{lines}", parse_mode='Markdown')

@bot.message_handler(commands=['broadcast'])
def broadcast(message):
    save_chat(message.chat)
    if not admin_db.is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Admin only.")
        return
    if message.chat.type != 'private':
        bot.reply_to(message, "⚠️ Use this command in DM.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "Usage: `/broadcast <text>`", parse_mode='Markdown')
        return
    text = parts[1].strip()
    chat_ids = get_all_target_chats()
    sent, failed = 0, 0
    for cid in chat_ids:
        try:
            bot.send_message(cid, f"📢 *Announcement*\n\n{text}", parse_mode='Markdown')
            sent += 1
        except Exception:
            failed += 1
    bot.reply_to(message, f"✅ Broadcast complete.\n\n📤 Sent: {sent}\n❌ Failed: {failed}")

@bot.message_handler(commands=['audience'])
def audience_cmd(message):
    save_chat(message.chat)
    private_count = 0
    group_count = 0

    if os.path.exists(PRIVATE_CHAT_DB):
        with open(PRIVATE_CHAT_DB) as f:
            private_count = len([line for line in f if line.strip()])
    if os.path.exists(GROUP_CHAT_DB):
        with open(GROUP_CHAT_DB) as f:
            group_count = len([line for line in f if line.strip()])

    total = private_count + group_count
    bot.reply_to(
        message,
        f"👥 *Bot Audience*\n"
        f"• Private users: `{private_count}`\n"
        f"• Groups: `{group_count}`\n"
        f"• Total unique chats: `{total}`",
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda m: m.text in MENU_BUTTONS)
def handle_menu_buttons(message):
    save_chat(message.chat)
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
    elif t == BTN_LISTADMINS:
        if admin_db.is_admin(message.from_user.id):
            admins_cmd(message)
    elif t == BTN_BROADCAST:
        if admin_db.is_admin(message.from_user.id):
            bot.reply_to(message, "Usage: `/broadcast <text>`", parse_mode='Markdown')
    elif t == BTN_ADDADMIN:
        if message.from_user.id == OWNER_ID:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("Add new admin", callback_data="add_admin"))
            bot.send_message(message.chat.id, "Press below to add a new admin by user ID:", reply_markup=markup)
    elif t == BTN_REMOVEADMIN:
        if message.from_user.id == OWNER_ID:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("Remove admin", callback_data="remove_admin"))
            bot.send_message(message.chat.id, "Press below to remove an admin by user ID:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "add_admin")
def ask_admin_id(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "⛔ Owner only.")
        return
    msg = bot.send_message(call.message.chat.id, "Send the Telegram user ID to add as an admin:")
    bot.register_next_step_handler(msg, process_new_admin_id)

def process_new_admin_id(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "⛔ Owner only.")
        return
    user_id = message.text.strip()
    if not user_id.isdigit():
        bot.reply_to(message, "User ID must be numeric. Please try again.")
        return
    admin_db.add_admin(user_id)
    bot.reply_to(message, f"✅ User `{user_id}` is now an admin.", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "remove_admin")
def ask_remove_admin_id(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "⛔ Owner only.")
        return
    msg = bot.send_message(call.message.chat.id, "Send the Telegram user ID to remove from admins:")
    bot.register_next_step_handler(msg, process_remove_admin_id)

def process_remove_admin_id(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "⛔ Owner only.")
        return
    user_id = message.text.strip()
    if not user_id.isdigit():
        bot.reply_to(message, "User ID must be numeric. Please try again.")
        return
    admin_db.remove_admin(user_id)
    bot.reply_to(message, f"✅ User `{user_id}` is no longer an admin.", parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.chat.type == 'private')
def dm_search(message):
    save_chat(message.chat)
    user_id = message.from_user.id
    if not admin_db.is_admin(user_id):
        bot_username = bot.get_me().username
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Start Bot in DM", url=f"https://t.me/{bot_username}?start=true"))
        bot.reply_to(message,
                     "⛔ *Access Denied.*\nPlease ask an admin to add you first.",
                     reply_markup=markup, parse_mode='Markdown')
        return
    do_search(message, message.text)

@bot.callback_query_handler(func=lambda call: call.data.startswith('trend_'))
def handle_trending_pick(call):
    save_chat(call.message.chat)
    query = call.data[len('trend_'):]
    stats['searches'] += 1
    search_counts[query.lower()] = search_counts.get(query.lower(), 0) + 1
    try:
        res = requests.get(f"https://lrclib.net/api/search?q={query}")
        results = res.json()
        query_low = query.strip().lower()
        filtered = [track for track in results
                    if query_low in track['trackName'].lower() or query_low in track['artistName'].lower()]
        display_tracks = filtered if filtered else results[:6]
        if not display_tracks:
            bot.answer_callback_query(call.id, "No results found.")
            return
        markup = types.InlineKeyboardMarkup()
        for track in display_tracks[:6]:
            btn_text = f"{track['artistName']} — {track['trackName']}"
            markup.add(types.InlineKeyboardButton(text=btn_text, callback_data=f"sel_{track['id']}"))
        bot.send_message(call.message.chat.id, f"🔥 Results for *{query.title()}*:",
                         reply_markup=markup, parse_mode='Markdown')
    except Exception:
        import traceback
        print(traceback.format_exc())
        bot.answer_callback_query(call.id, "Connection error.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('sel_'))
def show_options(call):
    save_chat(call.message.chat)
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

def send_lyrics_text(bot, chat_id, text, parse_mode=None):
    # Split and send safely on line boundaries, for Markdown or plain
    max_len = 4000 if parse_mode == "MarkdownV2" else 4096
    lines = text.splitlines(keepends=True)
    chunks, current = [], ""
    for line in lines:
        if len(current) + len(line) > max_len and current:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    for chunk in chunks:
        if parse_mode:
            bot.send_message(chat_id, chunk, parse_mode=parse_mode)
        else:
            bot.send_message(chat_id, chunk)

@bot.callback_query_handler(func=lambda call: call.data.startswith(('sf_', 'st_', 'pf_', 'pt_')))
def deliver_lyrics(call):
    save_chat(call.message.chat)
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
            def escape_markdown_v2_block(txt):
                chars = r"\_*[]()~`>#+-=|{}.!<>"
                return "".join("\\" + c if c in chars else c for c in txt)
            escaped = escape_markdown_v2_block(data)
            send_lyrics_text(bot, call.message.chat.id, f"`{escaped}`", parse_mode='MarkdownV2')
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
            send_lyrics_text(bot, call.message.chat.id, data)
        else:
            bot.answer_callback_query(call.id, "Not available.")

@bot.callback_query_handler(func=lambda call: call.data == "random")
def random_callback(call):
    save_chat(call.message.chat)
    stats['random_used'] += 1
    query = random.choice(RANDOM_QUERIES)
    try:
        res = requests.get(f"https://lrclib.net/api/search?q={query}")
        results = res.json()
        query_low = query.strip().lower()
        filtered = [track for track in results
                    if query_low in track['trackName'].lower() or query_low in track['artistName'].lower()]
        display_tracks = filtered if filtered else results[:6]
        if not display_tracks:
            bot.answer_callback_query(call.id, "Nothing found, try again.")
            return
        pick = random.choice(display_tracks)
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
    except Exception:
        import traceback
        print(traceback.format_exc())
        bot.answer_callback_query(call.id, "Connection error. Try again.")

@bot.callback_query_handler(func=lambda call: call.data == "back")
def back(call):
    save_chat(call.message.chat)
    bot.edit_message_text("Type a song name to search, or use the menu below.",
                          call.message.chat.id, call.message.message_id)

# --- START ---
bot_thread = Thread(target=start_bot)
bot_thread.daemon = True
bot_thread.start()

port = int(os.environ.get('PORT', 8080))
app.run(host='0.0.0.0', port=port)