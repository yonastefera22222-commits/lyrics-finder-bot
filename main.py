# final main.py (full)
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

from metadata_handler import read_id3v2_metadata, embed_lyrics_in_file

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

# --- In-memory state for Back functionality ---
# maps (chat_id, message_id) -> {'text': str, 'markup_rows': list or None}
previous_message_state = {}

# UI constants
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

def normalize_str(s):
    if not s:
        return ""
    s = str(s).lower().strip()
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r"[^\w\s&'\-]", '', s)
    return s

def escape_markdown_v2(text):
    if text is None:
        return ""
    chars = r"_*[]()~`>#+-=|{}.!<>"
    return "".join("\\" + c if c in chars else c for c in str(text))

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

def format_millis(ms):
    try:
        if ms is None:
            return None
        secs = int(ms // 1000)
        return f"{secs // 60}:{secs % 60:02d}"
    except Exception:
        return None

def serialize_markup(markup):
    """Convert InlineKeyboardMarkup into a serializable list of rows of button dicts."""
    if not markup:
        return None
    rows = []
    try:
        # telebot InlineKeyboardMarkup exposes .keyboard (list of lists)
        for row in getattr(markup, 'keyboard', []):
            row_list = []
            for btn in row:
                btn_obj = {}
                if getattr(btn, 'text', None) is not None:
                    btn_obj['text'] = btn.text
                if getattr(btn, 'callback_data', None) is not None:
                    btn_obj['callback_data'] = btn.callback_data
                if getattr(btn, 'url', None) is not None:
                    btn_obj['url'] = btn.url
                row_list.append(btn_obj)
            rows.append(row_list)
    except Exception as e:
        print(f"[DEBUG] serialize_markup error: {e}")
        return None
    return rows

def deserialize_markup(rows):
    """Rebuild InlineKeyboardMarkup from serialized rows."""
    if not rows:
        return None
    try:
        markup = types.InlineKeyboardMarkup()
        for row in rows:
            buttons = []
            for b in row:
                if 'callback_data' in b:
                    buttons.append(types.InlineKeyboardButton(b['text'], callback_data=b['callback_data']))
                elif 'url' in b:
                    buttons.append(types.InlineKeyboardButton(b['text'], url=b['url']))
                else:
                    buttons.append(types.InlineKeyboardButton(b['text']))
            markup.row(*buttons)
        return markup
    except Exception as e:
        print(f"[DEBUG] deserialize_markup error: {e}")
        return None

def save_prev_state(chat_id, message_id, text, reply_markup):
    try:
        rows = serialize_markup(reply_markup)
        previous_message_state[(chat_id, message_id)] = {
            'text': text or "",
            'markup_rows': rows
        }
    except Exception as e:
        print(f"[DEBUG] save_prev_state error: {e}")

def pop_prev_state(chat_id, message_id):
    return previous_message_state.pop((chat_id, message_id), None)

def find_lrclib_match_for_itunes(artist, title):
    """
    Given artist and title from iTunes, attempt to find a matching lrclib track.
    Returns the lrclib track dict or None.
    """
    try:
        if not title:
            return None
        q_full = f"{artist} {title}".strip() if artist else title
        try:
            q_enc = requests.utils.quote(q_full)[:200]
            res = requests.get(f"https://lrclib.net/api/search?q={q_enc}", timeout=6)
            results = res.json() if res.status_code == 200 else []
        except Exception:
            results = []
        artist_n = normalize_str(artist)
        title_n = normalize_str(title)
        for track in results:
            ta = normalize_str(track.get('artistName', ''))
            tt = normalize_str(track.get('trackName', ''))
            if title_n and tt == title_n and (not artist_n or ta == artist_n):
                return track
        for track in results:
            ta = normalize_str(track.get('artistName', ''))
            tt = normalize_str(track.get('trackName', ''))
            if title_n and (title_n in tt or tt in title_n):
                return track
        try:
            q_title_enc = requests.utils.quote(title)[:200]
            res2 = requests.get(f"https://lrclib.net/api/search?q={q_title_enc}", timeout=6)
            results2 = res2.json() if res2.status_code == 200 else []
        except Exception:
            results2 = []
        for track in results2:
            tt = normalize_str(track.get('trackName', ''))
            if title_n and (tt == title_n or title_n in tt or tt in title_n):
                ta = normalize_str(track.get('artistName', ''))
                if not artist_n or artist_n in ta or ta in artist_n:
                    return track
        return None
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
    try:
        bot.infinity_polling(skip_pending=True, timeout=30)
    except KeyboardInterrupt:
        print("[INFO] Bot interrupted")
    except Exception as e:
        print(f"[ERROR] Bot error: {e}")
        sys.exit(1)

# --- CONFIGURATION ---
API_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
if not API_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN environment variable")

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

def get_requester(message):
    if message.chat.type in ['group', 'supergroup']:
        user = message.from_user
        name = user.first_name
        if user.last_name:
            name += f" {user.last_name}"
        return f"👤 Requested by: *{name}*\n"
    return ""

# --- Search functions ---
def search_lyrics_by_metadata(artist, title, duration=None):
    """Search lrclib for lyrics"""
    try:
        artist = artist.strip() if artist else ""
        title = title.strip() if title else ""
        
        if not artist and not title:
            return None
        
        query = f"{artist} {title}".strip()
        res = requests.get(f"https://lrclib.net/api/search?q={query[:100]}", timeout=10)
        results = res.json()
        if not results:
            res = requests.get(f"https://lrclib.net/api/search?q={title[:100]}", timeout=10)
            results = res.json()
        if not results:
            return None
        
        artist_lower = artist.lower().strip()
        title_lower = title.lower().strip()
        
        for track in results:
            track_artist = track.get('artistName', '').lower().strip()
            track_title = track.get('trackName', '').lower().strip()
            if artist_lower == track_artist and title_lower == track_title:
                return track
        for track in results:
            track_artist = track.get('artistName', '').lower().strip()
            track_title = track.get('trackName', '').lower().strip()
            if (artist_lower in track_artist or track_artist in artist_lower) and \
               (title_lower in track_title or track_title in title_lower):
                return track
        return results[0]
    except Exception as e:
        print(f"[ERROR] Search error: {e}")
        import traceback; traceback.print_exc()
        return None

def get_lyrics_by_track_id(track_id):
    """Fetch lyrics with caching"""
    if not track_id:
        return None
    cached = lyrics_cache.get(track_id)
    if cached:
        return cached
    try:
        res = requests.get(f"https://lrclib.net/api/get/{track_id}", timeout=10)
        data = res.json()
        if data and 'id' in data:
            lyrics_cache.set(track_id, data)
            return data
        return None
    except Exception as e:
        print(f"[ERROR] Fetch error: {e}")
        import traceback; traceback.print_exc()
        return None

def do_search(message, query):
    """iTunes-first search; replies to the original message with results"""
    save_chat(message.chat)
    stats['searches'] += 1
    requester = get_requester(message)
    query_key = query.lower()[:50]
    search_counts[query_key] = search_counts.get(query_key, 0) + 1

    # reply to user's message (so results thread to it)
    status = bot.reply_to(message, f"🔍 Searching for *{query[:40]}*...", parse_mode='Markdown')
    try:
        itunes_items = get_itunes_results(query, limit=ITUNES_LIMIT)
        matched_tracks = []
        seen_ids = set()
        if itunes_items:
            for item in itunes_items:
                artist = item.get('artistName') or ''
                title = item.get('trackName') or ''
                lrctrack = find_lrclib_match_for_itunes(artist, title)
                if lrctrack:
                    tid = lrctrack.get('id')
                    if tid and tid not in seen_ids:
                        matched_tracks.append(lrctrack)
                        seen_ids.add(tid)
                if len(matched_tracks) >= 6:
                    break

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
            sent = bot.send_message(message.chat.id, prompt, reply_markup=markup, parse_mode='Markdown', reply_to_message_id=message.message_id)
            save_prev_state(sent.chat.id, sent.message_id, sent.text, sent.reply_markup)
            return

        # fallback to lrclib search
        res = requests.get(f"https://lrclib.net/api/search?q={query[:50]}", timeout=10)
        results = res.json()[:10]
        query_low = query.strip().lower()
        filtered = [
            track for track in results
            if query_low in track.get('trackName', '').lower() or 
               query_low in track.get('artistName', '').lower()
        ][:6]
        display_tracks = filtered if filtered else results[:6]

        try:
            bot.delete_message(message.chat.id, status.message_id)
        except:
            pass

        if not display_tracks:
            bot.send_message(message.chat.id, "No results found.", reply_to_message_id=message.message_id)
            return

        markup = types.InlineKeyboardMarkup()
        for track in display_tracks:
            artist = track.get('artistName', 'Unknown')[:25]
            title = track.get('trackName', 'Unknown')[:25]
            btn_text = f"{artist} — {title}"
            markup.add(types.InlineKeyboardButton(text=btn_text, callback_data=f"sel_{track.get('id')}"))
        prompt = "Select a version:" if requester else "Results:"
        sent = bot.send_message(message.chat.id, prompt, reply_markup=markup, parse_mode='Markdown', reply_to_message_id=message.message_id)
        save_prev_state(sent.chat.id, sent.message_id, sent.text, sent.reply_markup)

    except Exception as e:
        print(f"[ERROR] Search exception: {e}")
        import traceback; traceback.print_exc()
        try:
            bot.delete_message(message.chat.id, status.message_id)
        except:
            pass
        try:
            bot.send_message(message.chat.id, "Connection error.", reply_to_message_id=message.message_id)
        except:
            pass

# Handlers

@bot.callback_query_handler(func=lambda call: call.data.startswith('sel_'))
def show_options(call):
    save_chat(call.message.chat)
    track_id = call.data.split('_')[1]
    t = get_lyrics_by_track_id(track_id)
    if not t:
        bot.answer_callback_query(call.id, "Error.")
        return
    # Save previous state (serialize markup)
    try:
        save_prev_state(call.message.chat.id, call.message.message_id, call.message.text or "", call.message.reply_markup)
    except Exception:
        pass

    info = f"🎵 *{escape_markdown_v2(t.get('artistName','Unknown')[:25])}*\n📝 {escape_markdown_v2(t.get('trackName','Unknown')[:25])}"
    markup = types.InlineKeyboardMarkup(row_width=2)
    if t.get('syncedLyrics'):
        markup.add(types.InlineKeyboardButton("Syn→File", callback_data=f"sf_{track_id}"),
                   types.InlineKeyboardButton("Syn→Text", callback_data=f"st_{track_id}"))
    if t.get('plainLyrics'):
        markup.add(types.InlineKeyboardButton("Pln→File", callback_data=f"pf_{track_id}"),
                   types.InlineKeyboardButton("Pln→Text", callback_data=f"pt_{track_id}"))
    markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back"))
    try:
        bot.edit_message_text(info, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')
    except Exception:
        sent = bot.send_message(call.message.chat.id, info, reply_markup=markup, parse_mode='Markdown')
        save_prev_state(sent.chat.id, sent.message_id, sent.text, sent.reply_markup)
    gc.collect()

@bot.callback_query_handler(func=lambda call: call.data == "back")
def back(call):
    save_chat(call.message.chat)
    prev = pop_prev_state(call.message.chat.id, call.message.message_id)
    if prev:
        text = prev.get('text') or " "
        rows = prev.get('markup_rows')
        markup = deserialize_markup(rows) if rows else None
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
            return
        except Exception as e:
            print(f"[DEBUG] back restore failed edit: {e}")
            try:
                bot.send_message(call.message.chat.id, text, reply_markup=markup)
                return
            except Exception as e2:
                print(f"[DEBUG] back restore failed send: {e2}")
    # fallback
    try:
        bot.edit_message_text("Use menu below.", call.message.chat.id, call.message.message_id)
    except:
        try:
            bot.send_message(call.message.chat.id, "Use menu below.")
        except:
            pass
    gc.collect()

@bot.callback_query_handler(func=lambda call: call.data.startswith(('sf_', 'st_', 'pf_', 'pt_')))
def deliver_lyrics(call):
    save_chat(call.message.chat)
    parts = call.data.split('_')
    cmd = parts[0]
    track_id = parts[1]
    t = get_lyrics_by_track_id(track_id)
    if not t:
        bot.answer_callback_query(call.id, "Error.")
        return
    # Save state for Back
    try:
        save_prev_state(call.message.chat.id, call.message.message_id, call.message.text or "", call.message.reply_markup)
    except:
        pass

    name = f"{t.get('artistName','Unknown')[:20]} - {t.get('trackName','Unknown')[:20]}"

    if cmd == "sf":  # synced file
        data = t.get('syncedLyrics')
        if not data:
            bot.answer_callback_query(call.id, "Not available.")
            return
        try:
            bot.edit_message_text("⏳ Preparing lyrics file...", call.message.chat.id, call.message.message_id)
        except:
            pass
        bio = io.BytesIO(data.encode('utf-8'))
        bio.name = f"{name}.lrc"
        try:
            bot.send_document(call.message.chat.id, bio)
            done_markup = types.InlineKeyboardMarkup()
            done_markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back"))
            bot.edit_message_text(f"✅ Sent synced lyrics file: *{escape_markdown_v2(name)}*", call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=done_markup)
        except Exception as e:
            print(f"[ERROR] sending synced file: {e}")
            try:
                bot.edit_message_text("❌ Failed to send file.", call.message.chat.id, call.message.message_id)
            except:
                pass
        finally:
            try: bio.close()
            except: pass

    elif cmd == "st":  # synced text
        data = t.get('syncedLyrics')
        if data and len(data) <= 4000:
            try:
                bot.edit_message_text(f"`{data}`", call.message.chat.id, call.message.message_id, parse_mode='Markdown')
            except:
                bot.send_message(call.message.chat.id, f"`{data[:4000]}`", parse_mode='Markdown')
        elif data:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("Send as file", callback_data=f"sf_{track_id}"))
            markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back"))
            try:
                bot.edit_message_text("⚠️ Too long for text. Use file option?", call.message.chat.id, call.message.message_id, reply_markup=markup)
            except:
                bot.send_message(call.message.chat.id, "⚠️ Too long for text. Use file option?")
        else:
            bot.answer_callback_query(call.id, "Not available.")

    elif cmd == "pf":  # plain file
        data = t.get('plainLyrics')
        if not data:
            bot.answer_callback_query(call.id, "Not available.")
            return
        try:
            bot.edit_message_text("⏳ Preparing lyrics file...", call.message.chat.id, call.message.message_id)
        except:
            pass
        bio = io.BytesIO(data.encode('utf-8'))
        bio.name = f"{name}.txt"
        try:
            bot.send_document(call.message.chat.id, bio)
            done_markup = types.InlineKeyboardMarkup()
            done_markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back"))
            bot.edit_message_text(f"✅ Sent plain lyrics file: *{escape_markdown_v2(name)}*", call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=done_markup)
        except Exception as e:
            print(f"[ERROR] sending plain file: {e}")
            try:
                bot.edit_message_text("❌ Failed to send file.", call.message.chat.id, call.message.message_id)
            except:
                pass
        finally:
            try: bio.close()
            except: pass

    elif cmd == "pt":  # plain text
        data = t.get('plainLyrics')
        if data and len(data) <= 4000:
            try:
                bot.edit_message_text(data[:4000], call.message.chat.id, call.message.message_id)
            except:
                bot.send_message(call.message.chat.id, data[:4000])
        elif data:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("Send as file", callback_data=f"pf_{track_id}"))
            markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back"))
            try:
                bot.edit_message_text("⚠️ Too long for text. Use file option?", call.message.chat.id, call.message.message_id, reply_markup=markup)
            except:
                bot.send_message(call.message.chat.id, "⚠️ Too long for text. Use file option?")
        else:
            bot.answer_callback_query(call.id, "Not available.")
    gc.collect()

@bot.callback_query_handler(func=lambda call: call.data.startswith('prepare_embed_'))
def prepare_embed_callback(call):
    """Edit same message to show album & duration plus format choices"""
    save_chat(call.message.chat)
    parts = call.data.split('_')
    file_name = parts[2]
    track_id = parts[3]
    lyrics_data = get_lyrics_by_track_id(track_id)
    if not lyrics_data:
        bot.answer_callback_query(call.id, "Error fetching lyrics.")
        return

    # Save previous state
    try:
        save_prev_state(call.message.chat.id, call.message.message_id, call.message.text or "", call.message.reply_markup)
    except:
        pass

    album = lyrics_data.get('albumName') or lyrics_data.get('album') or "Unknown"
    duration_text = None
    if lyrics_data.get('trackTimeMillis'):
        duration_text = format_millis(lyrics_data.get('trackTimeMillis'))
    elif lyrics_data.get('duration'):
        try:
            dur = int(lyrics_data.get('duration'))
            duration_text = f"{dur//60}:{dur%60:02d}"
        except:
            duration_text = None

    # enrich from iTunes if missing
    if (not album or album == "Unknown") or (not duration_text):
        artist = lyrics_data.get('artistName') or lyrics_data.get('artist') or ""
        title = lyrics_data.get('trackName') or lyrics_data.get('title') or ""
        if title:
            try:
                itunes_items = get_itunes_results(f"{artist} {title}", limit=5)
                found_item = None
                artist_n = normalize_str(artist)
                title_n = normalize_str(title)
                for item in itunes_items:
                    ia = normalize_str(item.get('artistName',''))
                    it = normalize_str(item.get('trackName',''))
                    if ia == artist_n and it == title_n:
                        found_item = item
                        break
                if not found_item and itunes_items:
                    found_item = itunes_items[0]
                if found_item:
                    if not album or album == "Unknown":
                        album = found_item.get('collectionName') or album
                    if not duration_text and found_item.get('trackTimeMillis'):
                        duration_text = format_millis(found_item.get('trackTimeMillis'))
            except Exception as e:
                print(f"[DEBUG] iTunes enrich failed: {e}")

    if not duration_text:
        duration_text = "Unknown"

    has_synced = bool(lyrics_data.get('syncedLyrics'))
    has_plain = bool(lyrics_data.get('plainLyrics'))

    markup = types.InlineKeyboardMarkup(row_width=2)
    if has_synced:
        markup.add(types.InlineKeyboardButton("✨ Synced", callback_data=f"do_embed_{file_name}_{track_id}_synced"))
    if has_plain:
        markup.add(types.InlineKeyboardButton("✨ Plain", callback_data=f"do_embed_{file_name}_{track_id}_plain"))
    markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back"))

    artist_display = lyrics_data.get('artistName') or lyrics_data.get('artist') or "Unknown"
    title_display = lyrics_data.get('trackName') or lyrics_data.get('title') or "Unknown"
    info_lines = (
        f"🎵 *{escape_markdown_v2(artist_display)} — {escape_markdown_v2(title_display)}*\n"
        f"💿 Album: *{escape_markdown_v2(album)}*\n"
        f"⏱ Duration: *{escape_markdown_v2(duration_text)}*\n\n"
        "Choose lyrics type to embed:"
    )
    try:
        bot.edit_message_text(info_lines, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')
    except Exception:
        sent = bot.send_message(call.message.chat.id, info_lines, reply_markup=markup, parse_mode='Markdown')
        save_prev_state(sent.chat.id, sent.message_id, sent.text, sent.reply_markup)
    gc.collect()

@bot.callback_query_handler(func=lambda call: call.data.startswith('do_embed_'))
def do_embed_callback(call):
    """Embed lyrics into stored uploaded file; edit same message to show progress/status"""
    save_chat(call.message.chat)
    parts = call.data.split('_')
    file_name = parts[2]
    track_id = parts[3]
    lyrics_type = parts[4]

    # Save previous state
    try:
        save_prev_state(call.message.chat.id, call.message.message_id, call.message.text or "", call.message.reply_markup)
    except:
        pass

    try:
        file_info = admin_db.get_file_tracking(file_name)
        if not file_info:
            bot.edit_message_text("❌ File tracking not found.", call.message.chat.id, call.message.message_id)
            return
        file_path = file_info['file_path']
        if not os.path.exists(file_path):
            bot.edit_message_text("❌ File expired. Please send the music file again.", call.message.chat.id, call.message.message_id)
            return
        lyrics_data = get_lyrics_by_track_id(track_id)
        if lyrics_type == "synced":
            lyrics_content = lyrics_data.get('syncedLyrics')
        else:
            lyrics_content = lyrics_data.get('plainLyrics')
        if not lyrics_content:
            bot.edit_message_text("❌ Lyrics unavailable for this type.", call.message.chat.id, call.message.message_id)
            return

        bot.edit_message_text("⏳ Embedding lyrics into file...", call.message.chat.id, call.message.message_id)

        acquired = EMBED_SEMAPHORE.acquire(timeout=60)
        if not acquired:
            bot.edit_message_text("❌ Server busy. Try again shortly.", call.message.chat.id, call.message.message_id)
            return
        try:
            success = embed_lyrics_in_file(file_path, lyrics_content, lyrics_type)
        finally:
            try: EMBED_SEMAPHORE.release()
            except: pass

        if not success:
            bot.edit_message_text("❌ Failed to embed lyrics.", call.message.chat.id, call.message.message_id)
            return

        # send the file (separate message) then update status in edited message
        bot.edit_message_text("⏳ Sending lyricized file...", call.message.chat.id, call.message.message_id)
        time.sleep(1)
        artist = lyrics_data.get('artistName','Unknown')[:25]
        title = lyrics_data.get('trackName','Unknown')[:25]
        try:
            with open(file_path, 'rb') as f:
                bot.send_document(call.message.chat.id, f, caption=f"🎵 {artist} - {title}\n✨ Lyricized ({lyrics_type})!")
        except Exception as e:
            print(f"[ERROR] sending lyricized file: {e}")
            bot.edit_message_text("❌ Failed to send lyricized file.", call.message.chat.id, call.message.message_id)
            return

        done_markup = types.InlineKeyboardMarkup()
        done_markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back"))
        bot.edit_message_text(f"✅ Lyricized file sent: *{escape_markdown_v2(artist)} — {escape_markdown_v2(title)}*", call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=done_markup)
        stats['lyrics_delivered'] += 1
        gc.collect()
    except Exception as e:
        print(f"[ERROR] Embed error: {e}")
        import traceback; traceback.print_exc()
        try:
            bot.edit_message_text(f"❌ Error: {str(e)[:50]}", call.message.chat.id, call.message.message_id)
        except:
            pass
        gc.collect()

@bot.callback_query_handler(func=lambda call: call.data.startswith('view_lyrics_'))
def view_lyrics_callback(call):
    save_chat(call.message.chat)
    track_id = call.data.split('_')[2]
    lyrics_data = get_lyrics_by_track_id(track_id)
    if not lyrics_data:
        bot.answer_callback_query(call.id, "Error fetching.")
        return
    try:
        save_prev_state(call.message.chat.id, call.message.message_id, call.message.text or "", call.message.reply_markup)
    except:
        pass

    info = f"🎵 *{escape_markdown_v2(lyrics_data.get('artistName','Unknown')[:30])}*\n📝 {escape_markdown_v2(lyrics_data.get('trackName','Unknown')[:30])}"
    markup = types.InlineKeyboardMarkup(row_width=2)
    if lyrics_data.get('syncedLyrics'):
        markup.add(types.InlineKeyboardButton("Syn→File", callback_data=f"sf_{track_id}"),
                   types.InlineKeyboardButton("Syn→Text", callback_data=f"st_{track_id}"))
    if lyrics_data.get('plainLyrics'):
        markup.add(types.InlineKeyboardButton("Pln→File", callback_data=f"pf_{track_id}"),
                   types.InlineKeyboardButton("Pln→Text", callback_data=f"pt_{track_id}"))
    markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back"))
    try:
        bot.send_message(call.message.chat.id, info, reply_markup=markup, parse_mode='Markdown', reply_to_message_id=call.message.message_id)
    except:
        pass
    gc.collect()

# --- File upload handling (saves state so Back can restore) ---
@bot.message_handler(content_types=['audio', 'document'])
def handle_music_file(message):
    global last_cleanup
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

    # file size checks and retrieval
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
        downloaded_file = bot.download_file(file_info.file_path)
        safe_dir = f"music_{user_id}_{int(datetime.now().timestamp())}"
        tmpdir = os.path.join('/tmp', safe_dir)
        os.makedirs(tmpdir, exist_ok=True)
        ext = os.path.splitext(file_info.file_path)[1] or '.mp3'
        file_name = f"song{ext}"
        file_path = os.path.join(tmpdir, file_name)
        with open(file_path, 'wb') as f:
            f.write(downloaded_file)
        time.sleep(1)
        if not os.path.exists(file_path):
            bot.edit_message_text("❌ File save failed.", message.chat.id, progress_msg.message_id)
            return
        del downloaded_file
        gc.collect()

        bot.edit_message_text("📊 Extracting...", message.chat.id, progress_msg.message_id)
        metadata = read_id3v2_metadata(file_path)
        if not metadata:
            bot.edit_message_text("❌ Cannot read file metadata. Make sure the file has proper tags or is valid.", message.chat.id, progress_msg.message_id)
            try: os.remove(file_path)
            except: pass
            return

        artist = metadata.get('artist','Unknown')
        title = metadata.get('title','Unknown')
        bot.edit_message_text(f"🔍 Searching: *{artist[:20]} - {title[:20]}*...", message.chat.id, progress_msg.message_id, parse_mode='Markdown')
        track = search_lyrics_by_metadata(artist, title)
        if not track:
            bot.edit_message_text("❌ Lyrics not found for this song.", message.chat.id, progress_msg.message_id)
            try: os.remove(file_path)
            except: pass
            return

        track_id = track.get('id')
        lyrics_data = get_lyrics_by_track_id(track_id)
        if not lyrics_data:
            bot.edit_message_text("❌ Cannot fetch lyrics for this track.", message.chat.id, progress_msg.message_id)
            try: os.remove(file_path)
            except: pass
            return

        has_synced = bool(lyrics_data.get('syncedLyrics'))
        has_plain = bool(lyrics_data.get('plainLyrics'))
        if not has_synced and not has_plain:
            bot.edit_message_text("❌ No lyrics available for this track.", message.chat.id, progress_msg.message_id)
            try: os.remove(file_path)
            except: pass
            return

        try:
            bot.delete_message(message.chat.id, progress_msg.message_id)
        except:
            pass

        sent_markup = types.InlineKeyboardMarkup()
        sent_markup.row(
            types.InlineKeyboardButton("📄 View", callback_data=f"view_lyrics_{track_id}"),
            types.InlineKeyboardButton("🎵 Lyricize", callback_data=f"prepare_embed_{file_name}_{track_id}")
        )
        info_text = (f"✅ Found:\n\n"
                    f"🎵 *{escape_markdown_v2(lyrics_data.get('artistName','Unknown')[:30])}*\n"
                    f"📝 {escape_markdown_v2(lyrics_data.get('trackName','Unknown')[:30])}\n\n"
                    f"{'✓ Synced' if has_synced else '✗ Synced'}\n"
                    f"{'✓ Plain' if has_plain else '✗ Plain'}")
        sent = bot.send_message(message.chat.id, info_text, reply_markup=sent_markup, parse_mode='Markdown')
        save_prev_state(sent.chat.id, sent.message_id, sent.text, sent.reply_markup)
        admin_db.save_file_tracking(file_name, file_path, track_id)
        gc.collect()

    except Exception as e:
        print(f"[ERROR] File error: {e}")
        import traceback; traceback.print_exc()
        try:
            bot.edit_message_text(f"❌ Error: {str(e)[:50]}", message.chat.id, progress_msg.message_id)
        except:
            pass
        gc.collect()

# remaining admin and utility handlers (help, stats, broadcast, trending, random)
# (unchanged and omitted here for brevity — in your deployment include the same handlers from previous version)

# CLEANUP THREAD
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

# GRACEFUL SHUTDOWN
def signal_handler(sig, frame):
    print("[INFO] Shutting down gracefully...")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# START BOT
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