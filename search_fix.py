"""search_fix.py - small add-on for the Lyrics Finder Bot (main.py 1.4).

INSTALL (main.py must load this INSIDE its __main__ block, not at the very
bottom of the file - app.run() never returns, so a last-line import is dead
code).  main.py needs these 6 lines just above
"bot_thread = Thread(target=start_bot, daemon=True)":

    import sys
    sys.modules['main'] = sys.modules['__main__']
    try:
        import search_fix
    except Exception as e:
        print(f"[ERROR] search_fix could not be loaded: {e}")

Render logs must show:
    [INFO] search_fix loaded: groups, lyric search, copyable lyrics, uploads for everyone

WHAT IT FIXES / ADDS
  * tap-to-copy lyrics again - lyrics come back inside a code block, safely
    escaped so punctuation in a song can't break the message
  * groups: !song <name>, !find <words>, @yourbot <name>, and replies to the
    bot now work - and song uploads work in groups, so you can embed there
  * /lyrics and song uploads are no longer admin-only
  * search by lyrics: /find a line from the song  (needs a free GENIUS_TOKEN)
  * a search can never end in silence: the "Searching..." message is turned
    into the answer, so the result cannot just disappear
  * lyrics arrive as a REPLY to the message you searched with; when lyrics
    need several bubbles only the first one is a reply
  * when a search fails you get the real reason instead of "no lyrics found"

STILL NEEDED (Telegram setting, not code):
  In your group make the bot an ADMIN, otherwise Telegram never delivers normal
  group messages to it:  group name -> Edit -> Administrators -> Add Admin.
  /lyrics and /find work even without that.
"""

import io
import os
import re
import time

import main
from main import bot, admin_db, types

GENIUS_TOKEN = (os.environ.get('GENIUS_TOKEN') or '').strip()
RESTRICT_TO_ADMINS = os.environ.get('RESTRICT_TO_ADMINS', '0') == '1'
GROUP_PLAIN_TEXT = os.environ.get('GROUP_PLAIN_TEXT', '0') == '1'

SONG_PREFIXES = ('!song', '!lyrics', '!s', '!l', '!music')
FIND_PREFIXES = ('!find', '!f', '!words')
MENU = set(getattr(main, 'MENU_BUTTONS', ()) or ())


# ----------------------------------------------------------------- access
def allowed(user_id):
    """Everyone can use the bot unless RESTRICT_TO_ADMINS=1 is set."""
    if not RESTRICT_TO_ADMINS:
        return True
    try:
        return admin_db.is_admin(user_id)
    except Exception:
        return False


# ------------------------------------------------------- tap-to-copy lyrics
def code_block(text):
    """Telegram code block = one tap to copy. Backslashes/backticks escaped."""
    safe = str(text or "").replace("\\", "\\\\").replace("`", "\\`")
    return f"```\n{safe}\n```"


def send_copyable(chat_id, text, reply_to=None):
    text = (text or "").strip()
    if not text:
        return
    limit = 3200
    if len(text) <= limit:
        try:
            bot.send_message(chat_id, code_block(text), parse_mode='Markdown',
                             reply_to_message_id=reply_to)
            return
        except Exception as e:
            print(f"[DEBUG] code block failed, plain fallback: {e}")
            bot.send_message(chat_id, text, reply_to_message_id=reply_to)
            return
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    for i, chunk in enumerate(chunks[:4]):
        try:
            bot.send_message(chat_id, code_block(chunk.strip()), parse_mode='Markdown',
                             reply_to_message_id=reply_to if i == 0 else None)
        except Exception:
            bot.send_message(chat_id, chunk.strip(),
                             reply_to_message_id=reply_to if i == 0 else None)


def report(message, status, body, markup=None):
    """Show a result in place of the "Searching…" message, never silently drop it.

    Order of attempts:
      1. edit the "Searching…" message into the answer  (nothing disappears)
      2. delete it and send the answer as a reply to the user's message
      3. send the answer without a reply
    """
    chat_id = message.chat.id
    if status is not None:
        try:
            bot.edit_message_text(body, chat_id, status.message_id,
                                  reply_markup=markup)
            return
        except Exception as e:
            print(f"[DEBUG] search_fix: could not edit status message: {e}")
        try:
            bot.delete_message(chat_id, status.message_id)
        except Exception:
            pass
    try:
        bot.send_message(chat_id, body, reply_markup=markup,
                         reply_to_message_id=message.message_id)
        return
    except Exception as e:
        print(f"[DEBUG] search_fix: reply failed, sending plain: {e}")
    try:
        bot.send_message(chat_id, body, reply_markup=markup)
    except Exception as e:
        print(f"[ERROR] search_fix: could not report to user at all: {e}")


# ---------------------------------------------------------------- searching
def probe_lrclib():
    """Ask the lyrics service once, so a failed search can say why."""
    try:
        import requests
        res = requests.get(
            "https://lrclib.net/api/search", params={'q': 'adele hello'},
            timeout=8,
            headers={'User-Agent': 'LyricsFinderBot/1.1 (+https://lrclib.net)',
                     'Accept': 'application/json'})
        return "ok" if res.status_code == 200 else f"HTTP {res.status_code}"
    except Exception as e:
        return f"error: {e}"


def _guarded(fn, message, arg):
    """Never let a search die in silence - the user always sees something."""
    holder = {}
    try:
        fn(message, arg, holder)
    except Exception as e:
        print(f"[ERROR] search_fix: {fn.__name__} crashed: {e}")
        import traceback
        traceback.print_exc()
        try:
            report(message, holder.get('status'),
                   "\u26a0\ufe0f Something went wrong handling that search.\n"
                   "Please try again, or send /diag and show me the reply.")
        except Exception as e2:
            print(f"[ERROR] search_fix: could not even report the crash: {e2}")


def do_search(message, query):
    _guarded(_do_search, message, query)


def _do_search(message, query, holder):
    """Search, and if nothing comes back, say why."""
    try:
        main.save_chat(message.chat)
    except Exception:
        pass
    try:
        main.stats['searches'] += 1
    except Exception:
        pass

    chat_id = message.chat.id
    status = None
    try:
        status = bot.reply_to(
            message, f"\U0001f50d Searching for \u201c{(query or '')[:40]}\u201d\u2026")
    except Exception:
        status = None
    holder['status'] = status

    def clear():
        if status is None:
            return
        try:
            bot.delete_message(chat_id, status.message_id)
        except Exception:
            pass

    try:
        ranked = main.search_tracks(query, limit=8)
    except Exception as e:
        print(f"[ERROR] search_fix: search crashed: {e}")
        ranked = []

    if not ranked:
        # lrclib gave us nothing - try the backup lyrics source before we
        # tell the user we could not find anything.
        try:
            artist_part, title_part = main.split_artist_title(query)
            if not title_part:
                title_part, artist_part = (query or "").strip(), ""
            backup = fetch_lyrics_backup(artist_part, title_part)
        except Exception as e:
            print(f"[DEBUG] backup lookup error: {e}")
            backup = None

        if backup:
            track = make_backup_track(artist_part, title_part, backup)
            text, markup = main.build_track_options(track)
            report(message, status,
                   "ℹ️ Found the lyrics on the backup source.\n\n" + text,
                   markup=markup)
            return

        why = probe_lrclib()
        if why == "ok":
            report(message, status,
                   "\U0001f937 No lyrics found for that.\n\n"
                   "Try: Artist - Song   (e.g. Adele - Hello)\n"
                   "Or send me the audio file and tap \u2728 Lyricize.")
        else:
            report(message, status,
                   f"\u26a0\ufe0f The lyrics service isn't answering right now ({why}).\n\n"
                   "Please try again in a minute.\n"
                   "If it keeps happening, send /diag and show me the reply.")
        return

    try:
        main.remember_results(chat_id, ranked)
    except Exception:
        pass

    best_score, best = ranked[0]
    confident = best_score >= 88 and (best.get('plainLyrics') or best.get('syncedLyrics'))

    if len(ranked) == 1 or confident:
        text, markup = main.build_track_options(best)
        if len(ranked) > 1:
            markup.add(types.InlineKeyboardButton(
                f"\U0001f50e {len(ranked) - 1} other version{'s' if len(ranked) > 2 else ''}",
                callback_data="more"))
        report(message, status, text, markup=markup)
    else:
        report(message, status, main.results_text(ranked, query),
               markup=main.results_markup(ranked))


# ------------------------------------------- fallback lyrics source (no key)
def fetch_lyrics_backup(artist, title):
    """Plain lyrics from lyrics.ovh - used only when lrclib gives us nothing.

    If lrclib ever refuses our server (Cloudflare, rate limit, outage) this
    keeps the bot answering instead of showing "no lyrics found".
    """
    if not title:
        return None
    try:
        import requests
        from urllib.parse import quote
        url = ("https://api.lyrics.ovh/v1/"
               f"{quote((artist or '').strip())}/{quote(title.strip())}")
        res = requests.get(url, timeout=8,
                           headers={'User-Agent': 'LyricsFinderBot/1.5',
                                    'Accept': 'application/json'})
        if res.status_code == 200:
            text = ((res.json() or {}).get('lyrics') or '').strip()
            if text:
                print(f"[DEBUG] backup lyrics source worked for {artist} - {title}")
                return text
    except Exception as e:
        print(f"[DEBUG] backup lyrics source failed: {e}")
    return None


def make_backup_track(artist, title, lyrics):
    """Wrap fallback lyrics in a track the normal buttons can handle."""
    key = f"{artist}|{title}".lower()
    track_id = "bk" + str(abs(hash(key)) % (10 ** 9))
    track = {
        'id': track_id,
        'artistName': artist or 'Unknown',
        'trackName': title,
        'albumName': '',
        'duration': 0,
        'plainLyrics': lyrics,
        'syncedLyrics': None,
    }
    try:
        main.track_cache[track_id] = track      # so the buttons load it instantly
    except Exception:
        pass
    return track


# ------------------------------------------------------- search by lyrics
def genius_search(fragment, limit=5):
    """Words of a song -> [(artist, title), ...] using the free Genius API."""
    if not GENIUS_TOKEN:
        return None
    try:
        import requests
        res = requests.get(
            "https://api.genius.com/search", params={'q': fragment[:120]},
            headers={'Authorization': f'Bearer {GENIUS_TOKEN}',
                     'User-Agent': 'LyricsFinderBot/1.5'},
            timeout=8)
        if res.status_code != 200:
            print(f"[DEBUG] Genius -> HTTP {res.status_code}")
            return None
        hits = ((res.json() or {}).get('response') or {}).get('hits') or []
        pairs = []
        for hit in hits[:limit]:
            result = hit.get('result') or {}
            title = (result.get('title') or '').strip()
            artist = ((result.get('primary_artist') or {}).get('name') or '').strip()
            if title:
                pairs.append((artist, title))
        return pairs
    except Exception as e:
        print(f"[DEBUG] Genius search failed: {e}")
        return None


def do_lyric_fragment_search(message, fragment):
    _guarded(_do_lyric_fragment_search, message, fragment)


def _do_lyric_fragment_search(message, fragment, holder):
    try:
        main.save_chat(message.chat)
    except Exception:
        pass
    fragment = (fragment or "").strip()

    if len(fragment) < 8:
        bot.reply_to(message, "\U0001f50e Paste a longer line from the song - a few words at least.")
        return

    status = None
    try:
        status = bot.reply_to(message, "\U0001f50e Looking for the song with those words\u2026")
    except Exception:
        status = None
    holder['status'] = status

    def clear():
        if status is None:
            return
        try:
            bot.delete_message(message.chat.id, status.message_id)
        except Exception:
            pass

    if not GENIUS_TOKEN:
        report(message, status,
               "\U0001f50e Search-by-lyrics needs a free Genius token (about 2 minutes):\n\n"
               "1. Open genius.com/developers and sign in\n"
               "2. New API Client -> any name -> Save\n"
               "3. Copy the Access Token\n"
               "4. Render -> your service -> Environment -> add:\n"
               "     GENIUS_TOKEN = <your token>\n\n"
               "Searching by song name and sending the file both work without it.")
        return

    rows = []
    for artist, title in (genius_search(fragment) or []):
        ranked = main.search_tracks(f"{artist} {title}".strip(), limit=2)
        if ranked:
            rows.append(ranked[0][1])

    if not rows:
        report(message, status,
               "\U0001f937 No song matched those words.\n\n"
               "Try an exact line from the chorus, or just send the song name.")
        return

    try:
        main.remember_tracks(rows)
    except Exception:
        pass
    ranked = [(95 - i * 2, row) for i, row in enumerate(rows)]
    try:
        main.remember_results(message.chat.id, ranked)
    except Exception:
        pass

    text, markup = main.build_track_options(ranked[0][1])
    if len(ranked) > 1:
        markup.add(types.InlineKeyboardButton(
            f"\U0001f50e {len(ranked) - 1} other version{'s' if len(ranked) > 2 else ''}",
            callback_data="more"))
    report(message, status, text, markup=markup)


# ---------------------------------------------------------------- handlers
def _promote(collection):
    """Put the handler we just registered FIRST, so it wins over main.py's."""
    try:
        collection.insert(0, collection.pop())
    except Exception:
        pass


@bot.message_handler(commands=['lyrics', 'search', 'song', 'find', 'words'])
def _anyone_can_search(message):
    if not allowed(message.from_user.id):
        bot.reply_to(message, "\u26d4 Access Denied.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage:\n/lyrics Artist - Song\n/find a line from the song")
        return
    command = parts[0].split('@')[0].lower()
    text = parts[1].strip()
    if command in ('/find', '/words'):
        do_lyric_fragment_search(message, text[:200])
    else:
        do_search(message, text[:100])


_promote(bot.message_handlers)


@bot.message_handler(func=lambda m: m.chat.type == 'private'
                     and not (m.text or "").startswith('/'))
def _private_text(message):
    # commands are handled by the handler above, so never swallow them here
    text = (message.text or "").strip()
    if not text or text.startswith('/') or text in MENU or text.isdigit():
        return
    if not allowed(message.from_user.id):
        return
    do_search(message, text[:100])


_promote(bot.message_handlers)


@bot.message_handler(func=lambda m: m.chat.type in ('group', 'supergroup')
                     and not (m.text or "").startswith('/'))
def _group_text(message):
    # commands (/lyrics, /find) are handled by the handler above
    text = (message.text or "").strip()
    if not text or text.startswith('/') or text in MENU:
        return
    if not allowed(message.from_user.id):
        return

    reply = getattr(message, 'reply_to_message', None)
    from_user = getattr(reply, 'from_user', None) if reply is not None else None
    is_reply_to_bot = False
    if from_user is not None and getattr(from_user, 'is_bot', False):
        try:
            is_reply_to_bot = bot.get_me().id == from_user.id
        except Exception:
            is_reply_to_bot = False

    query = text
    try:
        username = (bot.get_me().username or "").strip()
        if username:
            stripped = re.sub(r"^\s*@?" + re.escape(username) + r"\b[:,]?\s*",
                              "", text, flags=re.I)
            mentioned = stripped != text
            query = stripped
        else:
            mentioned = False
    except Exception:
        mentioned = False

    head, _, rest = query.partition(' ')
    low = head.lower()
    if low in SONG_PREFIXES:
        query, find_mode = rest.strip(), False
    elif low in FIND_PREFIXES:
        query, find_mode = rest.strip(), True
    elif mentioned or is_reply_to_bot or GROUP_PLAIN_TEXT:
        find_mode = False
    else:
        return  # not meant for the bot - stay quiet

    if not query:
        bot.reply_to(message, "Type the song after the command, e.g.\n"
                              "!song Adele - Hello\n"
                              "!find I must have called a thousand times")
        return

    if find_mode:
        do_lyric_fragment_search(message, query[:200])
    else:
        do_search(message, query[:100])


_promote(bot.message_handlers)


@bot.message_handler(content_types=['audio', 'document', 'video', 'voice'])
def _uploads_for_everyone(message):
    """Let anyone send a song file (so embedding works in groups too)."""
    if not allowed(message.from_user.id):
        bot.reply_to(message, "\u26d4 Access Denied.")
        return
    if admin_db.is_admin(message.from_user.id):
        main.handle_music_file(message)
        return
    # borrow admin rights for this single call - main.py's upload handler
    # checks is_admin() internally and we do not want to rewrite all of it
    original = admin_db.is_admin
    admin_db.is_admin = lambda user_id: True
    try:
        main.handle_music_file(message)
    finally:
        admin_db.is_admin = original


_promote(bot.message_handlers)


@bot.callback_query_handler(func=lambda call: call.data.startswith(('sf_', 'st_', 'pf_', 'pt_')))
def _deliver_lyrics_copyable(call):
    """Send the lyrics as a tap-to-copy code block (or as a file)."""
    parts = call.data.split('_')
    if len(parts) < 2 or not parts[1]:
        return
    cmd, track_id = parts[0], parts[1]

    track = main.get_lyrics_by_track_id(track_id)
    if not track:
        try:
            bot.answer_callback_query(call.id, "Could not load that track - search again.")
        except Exception:
            pass
        return

    name = (f"{track.get('artistName', 'Unknown')[:20]}"
            f" - {track.get('trackName', 'Unknown')[:20]}")
    chat_id = call.message.chat.id

    # Answer in the same thread as the message the user searched with, so the
    # lyrics show up as a reply to their own text instead of a stray message.
    # The track card was sent as a reply, so its reply_to_message is the query.
    reply_to = None
    original = getattr(call.message, 'reply_to_message', None)
    if original is not None and getattr(original, 'message_id', None):
        reply_to = original.message_id
    else:
        reply_to = getattr(call.message, 'message_id', None)

    try:
        if cmd in ('sf', 'pf'):
            data = track.get('syncedLyrics') if cmd == 'sf' else track.get('plainLyrics')
            if not data:
                bot.answer_callback_query(call.id, "Not available.")
                return
            bio = io.BytesIO(data[:50000].encode('utf-8'))
            bio.name = f"{name}.lrc" if cmd == 'sf' else f"{name}.txt"
            bot.send_document(chat_id, bio, reply_to_message_id=reply_to)
            del bio
        else:
            data = track.get('syncedLyrics') if cmd == 'st' else track.get('plainLyrics')
            if not data:
                bot.answer_callback_query(
                    call.id,
                    "No synced lyrics for this one." if cmd == 'st' else "No plain lyrics for this one.")
                return
            # send_copyable replies on the FIRST bubble only; the rest follow on
            # as ordinary messages so the chat does not fill up with replies.
            send_copyable(chat_id, data, reply_to)
    except Exception as e:
        print(f"[ERROR] search_fix deliver failed: {e}")
        try:
            bot.answer_callback_query(call.id, "Something went wrong, try again.")
        except Exception:
            pass
        return

    try:
        main.stats['lyrics_delivered'] += 1
    except Exception:
        pass
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass


_promote(bot.callback_query_handlers)

print("[INFO] search_fix loaded: groups, lyric search, copyable lyrics, uploads for everyone")
