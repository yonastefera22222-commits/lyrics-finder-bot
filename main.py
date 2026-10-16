import os
import re
import io
import json
import time
import gc
import random
import signal
import sys
import requests
import telebot
from telebot import types
from flask import Flask
from threading import Thread, BoundedSemaphore
from datetime import datetime
from pathlib import Path
import tempfile
from rapidfuzz import fuzz

from metadata_handler import read_id3v2_metadata, embed_lyrics_in_file

# Load environment variables from .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# --- OWNER ID (set your Telegram user ID here) ---
OWNER_ID = 951232574  # <<<<<<------- OWNER ID SET HERE

# --- Group/Private Chat ID Storage for Broadcasts ---
GROUP_CHAT_DB = "group_chats.txt"
PRIVATE_CHAT_DB = "private_chats.txt"

# --- PERSISTENT FILE STORAGE (not temp) ---
FILE_STORAGE_DIR = "./music_files"
if not os.path.exists(FILE_STORAGE_DIR):
    os.makedirs(FILE_STORAGE_DIR)

# Concurrency limiter for embedding (protect Render Hobby resources)
EMBED_SEMAPHORE = BoundedSemaphore(2)

# --- MEMORY OPTIMIZATION: Reduce cache sizes ---
MAX_CACHE_SIZE = 20
CLEANUP_INTERVAL = 3600  # 60 minutes
FILE_EXPIRY_HOURS = 24  # 24 hours

# --- ITUNES CONFIG ---
ITUNES_COUNTRY = os.environ.get('ITUNES_COUNTRY', 'US')
ITUNES_LIMIT = int(os.environ.get('ITUNES_LIMIT', '8'))

def normalize_str(s):
    if not s:
        return ""
    s = str(s).lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^a-z0-9\s&'\-]", "", s)
    return s

def score_track_match(query_artist, query_title, track_artist, track_title):
    """Weighted fuzzy score for artist/title match."""
    query_artist = normalize_str(query_artist)
    query_title = normalize_str(query_title)
    track_artist = normalize_str(track_artist)
    track_title = normalize_str(track_title)

    if not query_title and not query_artist:
        return 0

    score = 0.0

    # Strongest weight: title
    if query_title and track_title:
        if query_title == track_title:
            score += 60
        else:
            score += max(0, fuzz.ratio(query_title, track_title) * 0.65)

    # Artist next
    if query_artist and track_artist:
        if query_artist == track_artist:
            score += 25
        else:
            score += max(0, fuzz.ratio(query_artist, track_artist) * 0.25)

    # Bonus for overlap on important words
    if query_title and track_title:
        q_tokens = set(query_title.split())
        t_tokens = set(track_title.split())
        overlap = len(q_tokens & t_tokens)
        if overlap:
            score += min(15, overlap * 5)

    # Bonus when one title contains the other
    if query_title and track_title:
        if query_title in track_title or track_title in query_title:
            score += 10

    # Bonus when artist contains query artist or vice versa
    if query_artist and track_artist:
        if query_artist in track_artist or track_artist in query_artist:
            score += 8

    return score

def get_itunes_results(query, country=ITUNES_COUNTRY, limit=ITUNES_LIMIT):
    """Query iTunes Search API for songs and return ordered list of results."""
    try:
        params = {
            'term': query,
            'media': 'music',
            'entity': 'song',
            'limit': limit,
            'country': country
        }
        res = requests.get("https://itunes.apple.com/search", params=params, timeout=6)
        data = res.json() if res.status_code == 200 else {}
        results = data.get('results', []) if isinstance(data, dict) else []
        return results
    except Exception as e:
        print(f"[DEBUG] iTunes query failed: {e}")
        return []

def find_lrclib_match_for_itunes(artist, title):
    """
    Given artist and title from iTunes, attempt to find the strongest
    lrclib match using weighted fuzzy scoring.
    """
    try:
        if not title:
            return None

        artist = artist or ""
        title = title or ""

        # Query 1: artist + title
        query_full = f"{artist} {title}".strip()
        results = []
        try:
            q_enc = requests.utils.quote(query_full)[:200]
            res = requests.get(f"https://lrclib.net/api/search?q={q_enc}", timeout=6)
            if res.status_code == 200:
                results = res.json() or []
        except Exception:
            results = []

        # Fallback: title only
        if not results:
            try:
                q_title_enc = requests.utils.quote(title)[:200]
                res2 = requests.get(f"https://lrclib.net/api/search?q={q_title_enc}", timeout=6)
                if res2.status_code == 200:
                    results = res2.json() or []
            except Exception:
                results = []

        if not results:
            return None

        ranked = []
        for track in results:
            track_artist = track.get("artistName", "")
            track_title = track.get("trackName", "")
            score = score_track_match(artist, title, track_artist, track_title)
            if score >= 55:
                ranked.append((score, track))

        if not ranked:
            return None

        ranked.sort(key=lambda x: x[0], reverse=True)
        return ranked[0][1]

    except Exception as e:
        print(f"[DEBUG] find_lrclib_match_for_itunes error: {e}")
        return None

def cleanup_old_files(max_age_hours=FILE_EXPIRY_HOURS):
    """Clean up old persistent files"""
    try:
        now = datetime.now().timestamp()
        for file in Path(FILE_STORAGE_DIR).glob("*"):
            if file.is_file() and (now - file.stat().st_mtime) > (max_age_hours * 3600):
                try:
                    file.unlink()
                    print(f"[DEBUG] Deleted expired file: {file.name}")
                except Exception as e:
                    print(f"[DEBUG] Could not delete {file.name}: {e}")
        gc.collect()
    except Exception as e:
        print(f"Cleanup error: {e}")

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
    try:
        with open(db_file, "r+") as f:
            ids = set(line.strip() for line in f)
            if chat_id not in ids:
                f.write(chat_id + "\n")
    except:
        pass

def get_all_target_chats():
    ids = set()
    if os.path.exists(PRIVATE_CHAT_DB):
        try:
            with open(PRIVATE_CHAT_DB) as f:
                ids.update(int(line.strip()) for line in f if line.strip())
        except:
            pass
    if os.path.exists(GROUP_CHAT_DB):
        try:
            with open(GROUP_CHAT_DB) as f:
                ids.update(int(line.strip()) for line in f if line.strip())
        except:
            pass
    return list(ids)

# --- FLASK KEEP-ALIVE ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive and running."

def start_bot():
    """Start bot with skip_pending to prevent conflict with other instances"""
    while True:
        try:
            # Remove any previously set webhook so long-polling can receive updates
            try:
                bot.delete_webhook(drop_pending_updates=True, timeout=15)
                print("[INFO] Webhook cleared; switching to long polling.")
            except Exception as e:
                print(f"[WARN] Could not clear webhook: {e}")
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
            break
        except KeyboardInterrupt:
            print("[INFO] Bot interrupted")
            break
        except Exception as e:
            print(f"[ERROR] Bot error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(5)
            continue

# --- CONFIGURATION ---
API_TOKEN = (
    os.environ.get('TELEGRAM_BOT_TOKEN')
    or os.environ.get('BOT_TOKEN')
    or os.environ.get('TELEGRAM_TOKEN')
    or os.environ.get('API_TOKEN')
)
if not API_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN (or BOT_TOKEN/TELEGRAM_TOKEN) environment variable")

bot = telebot.TeleBot(API_TOKEN, threaded=True, skip_pending=True)

# --- INIT ADMIN DB AND MIGRATE TEXT ADMINS ---
import admin_db
admin_db.setup_db()
if os.path.exists("authorized_users.txt"):
    try:
        with open("authorized_users.txt") as f:
            for uid in f.read().splitlines():
                if uid.strip().isdigit():
                    admin_db.add_admin(uid.strip())
    except:
        pass

# --- STATS (Memory efficient) ---
START_TIME = datetime.now()
stats = {"searches": 0, "lyrics_delivered": 0, "random_used": 0}
search_counts = {}
last_cleanup = datetime.now()

# --- LIGHTWEIGHT CACHE ---
class LightweightCache:
    def __init__(self, max_size=MAX_CACHE_SIZE):
        self.cache = {}
        self.max_size = max_size
        self.access_order = []

    def get(self, key):
        if key in self.cache:
            try:
                self.access_order.remove(key)
            except ValueError:
                pass
            self.access_order.append(key)
            return self.cache[key]
        return None

    def set(self, key, value):
        if len(self.cache) >= self.max_size:
            try:
                oldest = self.access_order.pop(0)
                del self.cache[oldest]
            except Exception:
                pass
            gc.collect()
        self.cache[key] = value
        if key in self.access_order:
            try:
                self.access_order.remove(key)
            except ValueError:
                pass
        self.access_order.append(key)

lyrics_cache = LightweightCache(MAX_CACHE_SIZE)

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
    chars = r"_*[]()~`>#+-=|{}.!<>"
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

def extract_music_metadata(file_path):
    """Extract metadata from music file"""
    try:
        metadata = read_id3v2_metadata(file_path)

        if not metadata:
            print(f"[DEBUG] No metadata found for {file_path}")
            return None

        print(f"[DEBUG] Metadata extracted: Artist={metadata['artist']}, Title={metadata['title']}")
        return metadata

    except Exception as e:
        print(f"[ERROR] Metadata error: {e}")
        import traceback
        traceback.print_exc()
        return None

def search_lyrics_by_metadata(artist, title, duration=None):
    """Search for lyrics efficiently using weighted fuzzy matching."""
    try:
        artist = (artist or "").strip()
        title = (title or "").strip()

        if not artist and not title:
            return None

        query = f"{artist} {title}".strip()
        print(f"[DEBUG] Searching for lyrics with query: {query}")

        results = []
        try:
            q = requests.utils.quote(query[:100])
            res = requests.get(f"https://lrclib.net/api/search?q={q}", timeout=10)
            if res.status_code == 200:
                results = res.json() or []
        except Exception:
            results = []

        if not results and title:
            try:
                q = requests.utils.quote(title[:100])
                res = requests.get(f"https://lrclib.net/api/search?q={q}", timeout=10)
                if res.status_code == 200:
                    results = res.json() or []
            except Exception:
                results = []

        if not results:
            return None

        ranked = []
        for track in results:
            track_artist = track.get("artistName", "")
            track_title = track.get("trackName", "")
            score = score_track_match(artist, title, track_artist, track_title)
            if score >= 55:
                ranked.append((score, track))

        if not ranked:
            print(f"[DEBUG] No strong match found; falling back to first result")
            return results[0]

        ranked.sort(key=lambda x: x[0], reverse=True)
        best = ranked[0][1]
        print(f"[DEBUG] Best match: {best.get('artistName')} - {best.get('trackName')}")
        return best

    except Exception as e:
        print(f"[ERROR] Search error: {e}")
        import traceback
        traceback.print_exc()
        return None

def get_lyrics_by_track_id(track_id):
    """Fetch lyrics with caching"""
    if not track_id:
        print("[ERROR] Track ID is empty")
        return None

    cached = lyrics_cache.get(track_id)
    if cached:
        print(f"[DEBUG] Using cached lyrics for track {track_id}")
        return cached

    try:
        print(f"[DEBUG] Fetching lyrics for track {track_id}")
        res = requests.get(
            f"https://lrclib.net/api/get/{track_id}",
            timeout=10
        )
        data = res.json()
        if data and 'id' in data:
            lyrics_cache.set(track_id, data)
            print(f"[DEBUG] Successfully fetched lyrics for track {track_id}")
            return data
        else:
            print(f"[ERROR] Invalid response for track {track_id}")
            return None
    except Exception as e:
        print(f"[ERROR] Fetch error: {e}")
        import traceback
        traceback.print_exc()
        return None

def do_search(message, query):
    """
    Primary search flow (iTunes-first):
    - Query iTunes for popular songs by the query term.
    - For each iTunes result (in order), try to find a matching lrclib track by artist+title.
    - Present the matched lrclib tracks in iTunes order (replying to the user's message).
    - Fallback to lrclib search if no iTunes→lrclib matches found.
    """
    save_chat(message.chat)
    stats['searches'] += 1
    requester = get_requester(message)
    query_key = query.lower()[:50]
    search_counts[query_key] = search_counts.get(query_key, 0) + 1

    # Post a temporary "Searching..." reply so we can reply-to the original message later
    status = bot.reply_to(message, f"🔍 Searching for *{query[:40]}*...", parse_mode='Markdown')
    try:
        # 1) Query iTunes first
        itunes_items = get_itunes_results(query, limit=ITUNES_LIMIT)
        matched_tracks = []
        seen_ids = set()
        if itunes_items:
            # For each iTunes item in popularity order, try to map to lrclib
            for item in itunes_items:
                artist = item.get('artistName') or ''
                title = item.get('trackName') or ''
                lrctrack = find_lrclib_match_for_itunes(artist, title)
                if lrctrack:
                    tid = lrctrack.get('id')
                    if tid and tid not in seen_ids:
                        matched_tracks.append(lrctrack)
                        seen_ids.add(tid)
                # Stop if we have enough to display
                if len(matched_tracks) >= 6:
                    break

        # If we have iTunes-backed matches, display them in iTunes order
        if matched_tracks:
            try:
                bot.delete_message(message.chat.id, status.message_id)
            except:
                pass

            markup = types.InlineKeyboardMarkup()
            for track in matched_tracks:
                artist = track.get('artistName', 'Unknown')[:25]
                title = track.get('trackName', 'Unknown')[:25]
                btn_text = f"{artist} — {title}"
                markup.add(types.InlineKeyboardButton(text=btn_text, callback_data=f"sel_{track.get('id')}"))
            prompt = "Top iTunes matches — select a version:" if requester else "Results:"
            bot.send_message(message.chat.id, prompt, reply_markup=markup, parse_mode='Markdown', reply_to_message_id=message.message_id)
            return

        # 2) Fallback: original lrclib search and ranking if no iTunes matches
        res = requests.get(f"https://lrclib.net/api/search?q={query[:50]}", timeout=10)
        results = res.json()[:10]

        query_low = query.strip().lower()
        filtered = [
            track for track in results
            if query_low in track.get('trackName', '').lower() or
               query_low in track.get('artistName', '').lower()
        ][:6]

        display_tracks = filtered if filtered else results[:6]

        if not display_tracks:
            try:
                bot.delete_message(message.chat.id, status.message_id)
            except:
                pass
            bot.send_message(message.chat.id, "No results found.", reply_to_message_id=message.message_id)
            return

        try:
            bot.delete_message(message.chat.id, status.message_id)
        except:
            pass

        markup = types.InlineKeyboardMarkup()
        for track in display_tracks:
            artist = track.get('artistName', 'Unknown')[:25]
            title = track.get('trackName', 'Unknown')[:25]
            btn_text = f"{artist} — {title}"
            markup.add(types.InlineKeyboardButton(text=btn_text, callback_data=f"sel_{track.get('id')}"))

        prompt = "Select a version:" if requester else "Results:"
        bot.send_message(message.chat.id, prompt, reply_markup=markup, parse_mode='Markdown', reply_to_message_id=message.message_id)
    except Exception as e:
        print(f"[ERROR] Search exception: {e}")
        import traceback
        traceback.print_exc()
        try:
            bot.delete_message(message.chat.id, status.message_id)
        except:
            pass
        try:
            bot.send_message(message.chat.id, "Connection error.", reply_to_message_id=message.message_id)
        except:
            pass

@bot.message_handler(commands=['start'])
def start(message):
    save_chat(message.chat)
    bot.send_message(
        message.chat.id,
        "✅ *Welcome!*\n\nSearch lyrics or send music files!\n\n"
        "_In DM: type song name_\n"
        "_In groups: /lyrics songname_",
        reply_markup=get_menu_markup(message.from_user.id),
        parse_mode='Markdown'
    )
    gc.collect()

@bot.message_handler(commands=['help'])
def help_cmd(message):
    save_chat(message.chat)
    bot.send_message(
        message.chat.id,
        "🎵 *Lyrics Bot Guide*\n\n"
        "🔹 *DM:* Type song name\n"
        "🔹 *Groups:* /lyrics songname\n"
        "🔹 *Send MP3:* I'll lyricize it!\n\n"
        "/random /trending /top /stats",
        reply_markup=get_menu_markup(message.from_user.id),
        parse_mode='Markdown'
    )
    gc.collect()

@bot.message_handler(commands=['stats'])
def send_stats(message):
    save_chat(message.chat)
    uptime = datetime.now() - START_TIME
    hours = uptime.total_seconds() // 3600
    text = (
        f"📊 *Stats*\n\n"
        f"🔍 Searches: {stats['searches']}\n"
        f"🎵 Delivered: {stats['lyrics_delivered']}\n"
        f"🎲 Random: {stats['random_used']}\n"
        f"⏱ Uptime: {int(hours)}h"
    )
    bot.reply_to(message, text, parse_mode='Markdown')
    gc.collect()

@bot.message_handler(commands=['top'])
def send_top(message):
    save_chat(message.chat)
    if not search_counts:
        bot.reply_to(message, "📭 No searches yet.")
        return
    sorted_songs = sorted(search_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = [f"{i+1}. {q} - {c}" for i, (q, c) in enumerate(sorted_songs)]
    text = "🏆 *Top Searches*\n\n" + "\n".join(lines)
    bot.reply_to(message, text, parse_mode='Markdown')
    gc.collect()

@bot.message_handler(commands=['trending'])
def send_trending(message):
    save_chat(message.chat)
    picks = random.sample(TRENDING_POOL, min(6, len(TRENDING_POOL)))
    markup = types.InlineKeyboardMarkup()
    for pick in picks:
        markup.add(types.InlineKeyboardButton(
            text=f"🔥 {pick[:30]}",
            callback_data=f"trend_{pick[:40]}"
        ))
    bot.send_message(message.chat.id, "🔥 *Trending*:", reply_markup=markup, parse_mode='Markdown')
    gc.collect()

@bot.message_handler(commands=['random'])
def random_song(message):
    save_chat(message.chat)
    stats['random_used'] += 1
    query = random.choice(RANDOM_QUERIES)
    status = bot.reply_to(message, f"🎲 Fetching *{query}*...", parse_mode='Markdown')
    try:
        res = requests.get(f"https://lrclib.net/api/search?q={query}", timeout=10)
        results = res.json()[:6]
        if not results:
            bot.edit_message_text("No results.", message.chat.id, status.message_id)
            return

        pick = random.choice(results)
        track_id = pick.get('id')
        t = get_lyrics_by_track_id(track_id)

        if not t:
            bot.edit_message_text("Error fetching track.", message.chat.id, status.message_id)
            return

        info = f"🎲 *{t.get('artistName', 'Unknown')[:30]} — {t.get('trackName', 'Unknown')[:30]}*\n💿 {t.get('albumName', 'N/A')[:30]}"

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("Synced File", callback_data=f"sf_{track_id}"),
            types.InlineKeyboardButton("Synced Text", callback_data=f"st_{track_id}"),
            types.InlineKeyboardButton("Plain File", callback_data=f"pf_{track_id}"),
            types.InlineKeyboardButton("Plain Text", callback_data=f"pt_{track_id}")
        )
        markup.add(types.InlineKeyboardButton("🎲 Another", callback_data="random"))
        bot.edit_message_text(info, message.chat.id, status.message_id,
                              reply_markup=markup, parse_mode='Markdown')
        gc.collect()
    except Exception as e:
        print(f"[ERROR] Random error: {e}")
        try:
            bot.edit_message_text("Error.", message.chat.id, status.message_id)
        except:
            pass

@bot.message_handler(commands=['lyrics', 'search'])
def lyrics_cmd(message):
    save_chat(message.chat)
    if not admin_db.is_admin(message.from_user.id):
        bot_username = bot.get_me().username
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Start Bot", url=f"https://t.me/{bot_username}?start=true"))
        bot.reply_to(message, "⛔ Access Denied.", reply_markup=markup, parse_mode='Markdown')
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: `/lyrics songname`", parse_mode='Markdown')
        return
    do_search(message, parts[1].strip()[:100])

@bot.message_handler(commands=['addadmin'])
def addadmin_cmd(message):
    save_chat(message.chat)
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "⛔ Owner only.")
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /addadmin userid")
        return
    admin_db.add_admin(parts[1])
    bot.reply_to(message, f"✅ User added.", parse_mode="Markdown")
    gc.collect()

@bot.message_handler(commands=['removeadmin'])
def removeadmin_cmd(message):
    save_chat(message.chat)
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "⛔ Owner only.")
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /removeadmin userid")
        return
    admin_db.remove_admin(parts[1])
    bot.reply_to(message, f"✅ User removed.", parse_mode="Markdown")
    gc.collect()

@bot.message_handler(commands=['admins'])
def admins_cmd(message):
    save_chat(message.chat)
    if not admin_db.is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Admin only.")
        return
    admin_list = admin_db.get_all_admins()
    if not admin_list:
        bot.reply_to(message, "📭 No admins.")
    else:
        lines = "\n".join([f"{i+1}. `{uid}`" for i, uid in enumerate(admin_list[:20])])
        bot.reply_to(message, f"👥 *Admins*\n\n{lines}", parse_mode='Markdown')
    gc.collect()

@bot.message_handler(commands=['broadcast'])
def broadcast(message):
    save_chat(message.chat)
    if not admin_db.is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Admin only.")
        return
    if message.chat.type != 'private':
        bot.reply_to(message, "⚠️ Use in DM.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: `/broadcast message`", parse_mode='Markdown')
        return
    text = parts[1][:200]
    chat_ids = get_all_target_chats()
    sent, failed = 0, 0
    for cid in chat_ids[:100]:
        try:
            bot.send_message(cid, f"📢 *Announcement*\n\n{text}", parse_mode='Markdown')
            sent += 1
        except:
            failed += 1
    bot.reply_to(message, f"✅ Sent: {sent} ❌ Failed: {failed}")
    gc.collect()

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
        markup.add(types.InlineKeyboardButton("Add Bot", url=f"https://t.me/{bot_username}?startgroup=true"))
        bot.reply_to(message, "Tap below:", reply_markup=markup)
    elif t == BTN_LISTADMINS:
        if admin_db.is_admin(message.from_user.id):
            admins_cmd(message)
    elif t == BTN_BROADCAST:
        if admin_db.is_admin(message.from_user.id):
            bot.reply_to(message, "Usage: `/broadcast msg`", parse_mode='Markdown')
    elif t == BTN_ADDADMIN:
        if message.from_user.id == OWNER_ID:
            msg = bot.send_message(message.chat.id, "Send user ID:")
            bot.register_next_step_handler(msg, process_new_admin_id)
    elif t == BTN_REMOVEADMIN:
        if message.from_user.id == OWNER_ID:
            msg = bot.send_message(message.chat.id, "Send user ID:")
            bot.register_next_step_handler(msg, process_remove_admin_id)
    gc.collect()

def process_new_admin_id(message):
    if message.from_user.id != OWNER_ID:
        return
    uid = message.text.strip()
    if uid.isdigit():
        admin_db.add_admin(uid)
        bot.reply_to(message, "✅ Added.")
    gc.collect()

def process_remove_admin_id(message):
    if message.from_user.id != OWNER_ID:
        return
    uid = message.text.strip()
    if uid.isdigit():
        admin_db.remove_admin(uid)
        bot.reply_to(message, "✅ Removed.")
    gc.collect()

@bot.message_handler(func=lambda m: m.chat.type == 'private')
def dm_search(message):
    save_chat(message.chat)
    if not admin_db.is_admin(message.from_user.id):
        bot_username = bot.get_me().username
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Start", url=f"https://t.me/{bot_username}?start=true"))
        bot.reply_to(message, "⛔ Access Denied.", reply_markup=markup, parse_mode='Markdown')
        return
    query = (message.text or "").strip()
    if not query:
        return
    do_search(message, query[:100])

# --- MUSIC FILE HANDLER ---
@bot.message_handler(content_types=['audio', 'document'])
def handle_music_file(message):
    global last_cleanup

    # Periodic cleanup
    now = datetime.now()
    if (now - last_cleanup).total_seconds() > CLEANUP_INTERVAL:
        cleanup_old_files()
        admin_db.cleanup_old_tracked_files(FILE_EXPIRY_HOURS)
        last_cleanup = now

    save_chat(message.chat)
    user_id = message.from_user.id

    if not admin_db.is_admin(user_id):
        bot_username = bot.get_me().username
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Start", url=f"https://t.me/{bot_username}?start=true"))
        bot.reply_to(message, "⛔ Access Denied.", reply_markup=markup, parse_mode='Markdown')
        return

    # Check file size (max 15MB)
    file_size = None
    if message.content_type == 'audio' and message.audio:
        file_size = message.audio.file_size
    elif message.content_type == 'document' and message.document:
        file_size = message.document.file_size

    if file_size and file_size > 15 * 1024 * 1024:
        bot.reply_to(message, "❌ File too large (max 15MB)")
        return

    file_info = None
    if message.content_type == 'audio':
        file_info = bot.get_file(message.audio.file_id)
    elif message.content_type == 'document':
        if message.document.mime_type and 'audio' in message.document.mime_type:
            file_info = bot.get_file(message.document.file_id)

    if not file_info:
        bot.reply_to(message, "❌ Invalid audio file.")
        return

    progress_msg = bot.reply_to(message, "⏳ Processing...")

    try:
        print(f"\n[DEBUG] ===== MUSIC FILE PROCESSING START =====")

        downloaded_file = bot.download_file(file_info.file_path)
        # Create unique per-request directory under /tmp to avoid collisions and follow ephemeral practice
        safe_dir = f"music_{user_id}_{int(datetime.now().timestamp())}"
        tmpdir = os.path.join('/tmp', safe_dir)
        os.makedirs(tmpdir, exist_ok=True)
        # Preserve original extension if present
        ext = os.path.splitext(file_info.file_path)[1] or '.mp3'
        file_name = f"song{ext}"
        file_path = os.path.join(tmpdir, file_name)

        print(f"[DEBUG] Saving to temp storage: {file_path}")

        with open(file_path, 'wb') as f:
            # downloaded_file is bytes
            f.write(downloaded_file)

        print(f"[DEBUG] File saved successfully")
        time.sleep(1)  # Ensure file is written

        # Verify file exists
        if not os.path.exists(file_path):
            print(f"[ERROR] File not saved: {file_path}")
            bot.edit_message_text("❌ File save failed.", message.chat.id, progress_msg.message_id)
            return

        del downloaded_file
        gc.collect()

        bot.edit_message_text("📊 Extracting...", message.chat.id, progress_msg.message_id)
        metadata = extract_music_metadata(file_path)

        if not metadata:
            print(f"[ERROR] Failed to extract metadata")
            bot.edit_message_text("❌ Cannot read file metadata. Make sure the file has proper tags or is valid.", message.chat.id, progress_msg.message_id)
            try:
                os.remove(file_path)
            except:
                pass
            print(f"[DEBUG] ===== MUSIC FILE PROCESSING END (FAILED) =====\n")
            return

        artist = metadata.get('artist', 'Unknown')
        title = metadata.get('title', 'Unknown')

        print(f"[DEBUG] Extracted: Artist={artist}, Title={title}")

        bot.edit_message_text(f"🔍 Searching: *{artist[:20]} - {title[:20]}*...",
                            message.chat.id, progress_msg.message_id, parse_mode='Markdown')

        track = search_lyrics_by_metadata(artist, title)

        if not track:
            print(f"[ERROR] Track not found in API")
            bot.edit_message_text("❌ Lyrics not found for this song.", message.chat.id, progress_msg.message_id)
            try:
                os.remove(file_path)
            except:
                pass
            return

        print(f"[DEBUG] Track found: {track}")
        track_id = track.get('id')
        if not track_id:
            print(f"[ERROR] Track has no ID")
            bot.edit_message_text("❌ Invalid track data.", message.chat.id, progress_msg.message_id)
            try:
                os.remove(file_path)
            except:
                pass
            return

        lyrics_data = get_lyrics_by_track_id(track_id)

        if not lyrics_data:
            print(f"[ERROR] Could not fetch lyrics data")
            bot.edit_message_text("❌ Cannot fetch lyrics for this track.", message.chat.id, progress_msg.message_id)
            try:
                os.remove(file_path)
            except:
                pass
            return

        has_synced = bool(lyrics_data.get('syncedLyrics'))
        has_plain = bool(lyrics_data.get('plainLyrics'))

        print(f"[DEBUG] Synced: {has_synced}, Plain: {has_plain}")

        if not has_synced and not has_plain:
            print(f"[ERROR] No lyrics available")
            bot.edit_message_text("❌ No lyrics available for this track.", message.chat.id, progress_msg.message_id)
            try:
                os.remove(file_path)
            except:
                pass
            return

        try:
            bot.delete_message(message.chat.id, progress_msg.message_id)
        except:
            pass

        # Save tracking info - store full path so embed callback can find it
        admin_db.save_file_tracking(file_name, file_path, track_id)

        info_text = (f"✅ Found:\n\n"
                    f"🎵 *{lyrics_data.get('artistName', 'Unknown')[:30]}*\n"
                    f"📝 {lyrics_data.get('trackName', 'Unknown')[:30]}\n\n"
                    f"{'✓ Synced' if has_synced else '✗ Synced'}\n"
                    f"{'✓ Plain' if has_plain else '✗ Plain'}")

        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("📄 View", callback_data=f"view_lyrics_{track_id}"),
            types.InlineKeyboardButton("🎵 Lyricize", callback_data=f"prepare_embed_{file_name}_{track_id}")
        )

        bot.send_message(message.chat.id, info_text, reply_markup=markup, parse_mode='Markdown')
        print(f"[DEBUG] ===== MUSIC FILE PROCESSING END (SUCCESS) =====\n")
        gc.collect()

    except Exception as e:
        print(f"[ERROR] File error: {e}")
        import traceback
        traceback.print_exc()
        try:
            bot.edit_message_text(f"❌ Error: {str(e)[:50]}", message.chat.id, progress_msg.message_id)
        except:
            pass
        print(f"[DEBUG] ===== MUSIC FILE PROCESSING END (EXCEPTION) =====\n")
        gc.collect()

@bot.callback_query_handler(func=lambda call: call.data.startswith('view_lyrics_'))
def view_lyrics_callback(call):
    save_chat(call.message.chat)
    track_id = call.data.split('_')[2]

    lyrics_data = get_lyrics_by_track_id(track_id)

    if not lyrics_data:
        bot.answer_callback_query(call.id, "Error fetching.")
        return

    info = f"🎵 *{lyrics_data.get('artistName', 'Unknown')[:30]}*\n📝 {lyrics_data.get('trackName', 'Unknown')[:30]}"

    markup = types.InlineKeyboardMarkup(row_width=2)

    if lyrics_data.get('syncedLyrics'):
        markup.add(
            types.InlineKeyboardButton("Syn→File", callback_data=f"sf_{track_id}"),
            types.InlineKeyboardButton("Syn→Text", callback_data=f"st_{track_id}")
        )
    if lyrics_data.get('plainLyrics'):
        markup.add(
            types.InlineKeyboardButton("Pln→File", callback_data=f"pf_{track_id}"),
            types.InlineKeyboardButton("Pln→Text", callback_data=f"pt_{track_id}")
        )

    bot.send_message(call.message.chat.id, info, reply_markup=markup, parse_mode='Markdown')
    gc.collect()

@bot.callback_query_handler(func=lambda call: call.data.startswith('prepare_embed_'))
def prepare_embed_callback(call):
    """Prepare embedding - show options"""
    save_chat(call.message.chat)
    parts = call.data.split('_')
    file_name = parts[2]
    track_id = parts[3]

    lyrics_data = get_lyrics_by_track_id(track_id)

    if not lyrics_data:
        bot.answer_callback_query(call.id, "Error fetching lyrics.")
        return

    has_synced = bool(lyrics_data.get('syncedLyrics'))
    has_plain = bool(lyrics_data.get('plainLyrics'))

    markup = types.InlineKeyboardMarkup()

    if has_synced:
        markup.add(types.InlineKeyboardButton("✨ Synced", callback_data=f"do_embed_{file_name}_{track_id}_synced"))
    if has_plain:
        markup.add(types.InlineKeyboardButton("✨ Plain", callback_data=f"do_embed_{file_name}_{track_id}_plain"))

    bot.send_message(call.message.chat.id, "Choose lyrics type to embed:", reply_markup=markup)
    gc.collect()

@bot.callback_query_handler(func=lambda call: call.data.startswith('do_embed_'))
def do_embed_callback(call):
    """Do actual embedding"""
    save_chat(call.message.chat)
    parts = call.data.split('_')
    file_name = parts[2]
    track_id = parts[3]
    lyrics_type = parts[4]

    msg = bot.send_message(call.message.chat.id, "⏳ Embedding lyrics...")

    try:
        # Get file path from database
        file_info = admin_db.get_file_tracking(file_name)
        if not file_info:
            bot.edit_message_text("❌ File tracking not found.", call.message.chat.id, msg.message_id)
            return

        file_path = file_info['file_path']

        # Verify file still exists
        if not os.path.exists(file_path):
            print(f"[ERROR] File not found: {file_path}")
            bot.edit_message_text("❌ File expired. Please send the music file again.", call.message.chat.id, msg.message_id)
            return

        lyrics_data = get_lyrics_by_track_id(track_id)

        if lyrics_type == "synced":
            lyrics_content = lyrics_data.get('syncedLyrics')
        else:
            lyrics_content = lyrics_data.get('plainLyrics')

        if not lyrics_content:
            bot.edit_message_text("❌ Lyrics unavailable for this type.", call.message.chat.id, msg.message_id)
            return

        bot.edit_message_text("⏳ Embedding lyrics into file...", call.message.chat.id, msg.message_id)

        # Acquire semaphore to limit concurrent embeddings
        acquired = EMBED_SEMAPHORE.acquire(timeout=60)
        if not acquired:
            bot.edit_message_text("❌ Server busy. Try again shortly.", call.message.chat.id, msg.message_id)
            return

        try:
            success = embed_lyrics_in_file(file_path, lyrics_content, lyrics_type)
        finally:
            try:
                EMBED_SEMAPHORE.release()
            except Exception:
                pass

        if not success:
            bot.edit_message_text("❌ Failed to embed lyrics.", call.message.chat.id, msg.message_id)
            return

        bot.edit_message_text("⏳ Sending lyricized file...", call.message.chat.id, msg.message_id)
        time.sleep(1)

        artist = lyrics_data.get('artistName', 'Unknown')[:25]
        title = lyrics_data.get('trackName', 'Unknown')[:25]

        with open(file_path, 'rb') as f:
            bot.send_document(call.message.chat.id, f, caption=f"🎵 {artist} - {title}\n✨ Lyricized ({lyrics_type})!")

        try:
            bot.delete_message(call.message.chat.id, msg.message_id)
        except:
            pass

        stats['lyrics_delivered'] += 1
        print(f"[DEBUG] File will expire in {FILE_EXPIRY_HOURS} hours")
        gc.collect()

    except Exception as e:
        print(f"[ERROR] Embed error: {e}")
        import traceback
        traceback.print_exc()
        try:
            bot.edit_message_text(f"❌ Error: {str(e)[:50]}", call.message.chat.id, msg.message_id)
        except:
            pass
        gc.collect()

@bot.callback_query_handler(func=lambda call: call.data.startswith('trend_'))
def handle_trending_pick(call):
    save_chat(call.message.chat)
    query = call.data[len('trend_'):][:50]
    stats['searches'] += 1
    try:
        res = requests.get(f"https://lrclib.net/api/search?q={query}", timeout=10)
        results = res.json()[:6]

        if not results:
            bot.answer_callback_query(call.id, "No results.")
            return

        markup = types.InlineKeyboardMarkup()
        for track in results:
            artist = track.get('artistName', 'Unknown')[:20]
            title = track.get('trackName', 'Unknown')[:20]
            markup.add(types.InlineKeyboardButton(text=f"{artist}—{title}", callback_data=f"sel_{track.get('id')}"))

        bot.send_message(call.message.chat.id, "Results:", reply_markup=markup)
        gc.collect()
    except Exception as e:
        print(f"[ERROR] Trending error: {e}")
        bot.answer_callback_query(call.id, "Error.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('sel_'))
def show_options(call):
    save_chat(call.message.chat)
    track_id = call.data.split('_')[1]

    t = get_lyrics_by_track_id(track_id)
    if not t:
        bot.answer_callback_query(call.id, "Error.")
        return

    info = f"🎵 *{t.get('artistName', 'Unknown')[:25]}*\n📝 {t.get('trackName', 'Unknown')[:25]}"

    markup = types.InlineKeyboardMarkup(row_width=2)
    if t.get('syncedLyrics'):
        markup.add(
            types.InlineKeyboardButton("Syn→File", callback_data=f"sf_{track_id}"),
            types.InlineKeyboardButton("Syn→Text", callback_data=f"st_{track_id}")
        )
    if t.get('plainLyrics'):
        markup.add(
            types.InlineKeyboardButton("Pln→File", callback_data=f"pf_{track_id}"),
            types.InlineKeyboardButton("Pln→Text", callback_data=f"pt_{track_id}")
        )
    markup.add(types.InlineKeyboardButton("⬅️", callback_data="back"))

    bot.edit_message_text(info, call.message.chat.id, call.message.message_id, reply_markup=markup)
    gc.collect()

@bot.callback_query_handler(func=lambda call: call.data.startswith(('sf_', 'st_', 'pf_', 'pt_')))
def deliver_lyrics(call):
    save_chat(call.message.chat)
    stats['lyrics_delivered'] += 1
    parts = call.data.split('_')
    cmd = parts[0]
    track_id = parts[1]

    t = get_lyrics_by_track_id(track_id)
    if not t:
        bot.answer_callback_query(call.id, "Error.")
        return

    name = f"{t.get('artistName', 'Unknown')[:20]} - {t.get('trackName', 'Unknown')[:20]}"

    if cmd == "sf":
        data = t.get('syncedLyrics')
        if data:
            bio = io.BytesIO(data[:50000].encode('utf-8'))
            bio.name = f"{name}.lrc"
            bot.send_document(call.message.chat.id, bio)
            del bio
        else:
            bot.answer_callback_query(call.id, "Not available.")
    elif cmd == "st":
        data = t.get('syncedLyrics')
        if data and len(data) <= 3500:
            bot.send_message(call.message.chat.id, f"`{data[:3500]}`", parse_mode='Markdown')
        elif data:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("Send as file", callback_data=f"sf_{track_id}"))
            bot.send_message(call.message.chat.id, "⚠️ Too long for text. Use file option?", reply_markup=markup)
        else:
            bot.answer_callback_query(call.id, "Not available.")
    elif cmd == "pf":
        data = t.get('plainLyrics')
        if data:
            bio = io.BytesIO(data[:50000].encode('utf-8'))
            bio.name = f"{name}.txt"
            bot.send_document(call.message.chat.id, bio)
            del bio
        else:
            bot.answer_callback_query(call.id, "Not available.")
    elif cmd == "pt":
        data = t.get('plainLyrics')
        if data and len(data) <= 4000:
            bot.send_message(call.message.chat.id, data[:4000])
        elif data:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("Send as file", callback_data=f"pf_{track_id}"))
            bot.send_message(call.message.chat.id, "⚠️ Too long for text. Use file option?", reply_markup=markup)
        else:
            bot.answer_callback_query(call.id, "Not available.")

    gc.collect()

@bot.callback_query_handler(func=lambda call: call.data == "random")
def random_callback(call):
    save_chat(call.message.chat)
    stats['random_used'] += 1
    query = random.choice(RANDOM_QUERIES)
    try:
        res = requests.get(f"https://lrclib.net/api/search?q={query}", timeout=10)
        results = res.json()[:6]
        if not results:
            bot.answer_callback_query(call.id, "Error.")
            return

        pick = random.choice(results)
        t = get_lyrics_by_track_id(pick.get('id'))
        if not t:
            bot.answer_callback_query(call.id, "Error.")
            return

        info = f"🎲 *{t.get('artistName', 'Unknown')[:25]}*\n📝 {t.get('trackName', 'Unknown')[:25]}"

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("Syn→F", callback_data=f"sf_{pick.get('id')}"),
            types.InlineKeyboardButton("Syn→T", callback_data=f"st_{pick.get('id')}"),
            types.InlineKeyboardButton("Pln→F", callback_data=f"pf_{pick.get('id')}"),
            types.InlineKeyboardButton("Pln→T", callback_data=f"pt_{pick.get('id')}")
        )
        markup.add(types.InlineKeyboardButton("🎲", callback_data="random"))

        bot.edit_message_text(info, call.message.chat.id, call.message.message_id,
                              reply_markup=markup, parse_mode='Markdown')
        gc.collect()
    except Exception as e:
        print(f"[ERROR] Random error: {e}")
        bot.answer_callback_query(call.id, "Error.")

@bot.callback_query_handler(func=lambda call: call.data == "back")
def back(call):
    save_chat(call.message.chat)
    bot.edit_message_text("Use menu below.", call.message.chat.id, call.message.message_id)
    gc.collect()

# --- CLEANUP THREAD ---
def cleanup_thread():
    while True:
        try:
            time.sleep(CLEANUP_INTERVAL)
            cleanup_old_files()
            admin_db.cleanup_old_tracked_files(FILE_EXPIRY_HOURS)
            gc.collect()
        except Exception as e:
            print(f"[ERROR] Cleanup error: {e}")

cleanup_t = Thread(target=cleanup_thread, daemon=True)
cleanup_t.daemon = True
cleanup_t.start()

# --- GRACEFUL SHUTDOWN ---
def signal_handler(sig, frame):
    print("[INFO] Shutting down gracefully...")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# --- START BOT ---
if __name__ == "__main__":
    print("[INFO] Starting Lyrics Finder Bot...")
    bot_thread = Thread(target=start_bot, daemon=True)
    bot_thread.daemon = True
    bot_thread.start()

    port = int(os.environ.get('PORT', 8080))
    print(f"[INFO] Flask running on port {port}")
    try:
        app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
    except KeyboardInterrupt:
        print("[INFO] Bot stopped")