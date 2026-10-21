"""search_fix.py - Artist - Song - Album search for the Lyrics Finder Bot.

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

Render logs must show BOTH of these lines:
    [INFO] search_fix: lrclib rate limiter installed
    [INFO] search_fix loaded: ...

HOW SEARCHING WORKS NOW
  * you can type  Artist - Song - Album   (any separator: - : | en-dash)
    e.g.  Adele - Hello - 25
  * you can also type  Artist - Song  or just  Song
  * if you DO give an album, the bot looks up that exact album
  * if you do NOT give an album, the bot lists every album version of the
    song it can find, one line per album
  * album name and track duration are shown on every answer: the list, the
    track card, and the header above the lyrics
  * iTunes is not used at all any more - every lookup goes to lrclib

STILL NEEDED (Telegram setting, not code):
  In your group make the bot an ADMIN, otherwise Telegram never delivers normal
  group messages to it:  group name -> Edit -> Administrators -> Add Admin.
  /lyrics and /find work even without that.
"""

import io
import os
import re
import threading
import time

import main
from main import bot, admin_db, types

GENIUS_TOKEN = (os.environ.get('GENIUS_TOKEN') or '').strip()
RESTRICT_TO_ADMINS = os.environ.get('RESTRICT_TO_ADMINS', '0') == '1'
GROUP_PLAIN_TEXT = os.environ.get('GROUP_PLAIN_TEXT', '0') == '1'
MAX_ALBUMS = int(os.environ.get('MAX_ALBUMS', '12'))

SONG_PREFIXES = ('!song', '!lyrics', '!s', '!l', '!music')
FIND_PREFIXES = ('!find', '!f', '!words')
MENU = set(getattr(main, 'MENU_BUTTONS', ()) or ())


# ------------------------------------------------- lrclib rate limiting
# lrclib answers HTTP 429 when a client fires several requests within a couple
# of seconds. main.py's lrclib_get() retries 3 times on anything that is not
# 200/400/404, so one search turned into six requests and the throttling just
# got worse. This replacement sends at most one request every MIN_GAP seconds
# and, on a 429, stops talking to lrclib for COOLDOWN seconds instead of
# retrying.
LRCLIB_HEADERS = getattr(main, 'LRCLIB_HEADERS', {
    'User-Agent': 'LyricsFinderBot/1.1 (+https://lrclib.net)',
    'Accept': 'application/json',
})
MIN_GAP = float(os.environ.get('LRCLIB_MIN_GAP', '1.2'))
COOLDOWN = float(os.environ.get('LRCLIB_COOLDOWN', '20'))

_lrclib_lock = threading.Lock()
_last_call = [0.0]
_last_status = [None]        # 'ok', 'HTTP 429', 'error: ...', None = nothing yet
_cooldown_until = [0.0]


def last_lrclib_status():
    """What lrclib last told us. Recorded for free - no extra request."""
    if time.time() < _cooldown_until[0]:
        return _last_status[0] or 'HTTP 429'
    return _last_status[0]


def _throttled_lrclib_get(url, params=None, timeout=10):
    """Rate-limited replacement for main.lrclib_get."""
    if time.time() < _cooldown_until[0]:
        return None                       # still cooling off - do not even ask
    with _lrclib_lock:
        wait = MIN_GAP - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
    try:
        import requests
        res = requests.get(url, params=params, headers=LRCLIB_HEADERS,
                           timeout=timeout)
    except Exception as e:
        _last_status[0] = f"error: {e}"
        return None
    if res.status_code == 200:
        try:
            _last_status[0] = "ok"
            return res.json()
        except Exception:
            _last_status[0] = "invalid JSON"
            return None
    if res.status_code == 404:
        _last_status[0] = "not found"     # normal: lrclib simply has no match
    else:
        _last_status[0] = f"HTTP {res.status_code}"
    if res.status_code == 429:
        _cooldown_until[0] = time.time() + COOLDOWN
        print(f"[WARN] lrclib rate-limited us (429); "
              f"pausing lrclib for {COOLDOWN:.0f}s")
    return None


try:
    main.lrclib_get = _throttled_lrclib_get
    print("[INFO] search_fix: lrclib rate limiter installed")
except Exception as e:
    print(f"[ERROR] search_fix: could not install lrclib limiter: {e}")


# ----------------------------------------------------------------- access
def allowed(user_id):
    """Everyone can use the bot unless RESTRICT_TO_ADMINS=1 is set."""
    if not RESTRICT_TO_ADMINS:
        return True
    try:
        return admin_db.is_admin(user_id)
    except Exception:
        return False


# ------------------------------------------------------- query parsing
# main.py's own last_results keeps only 8 rows, so the album list gets its own
# store (it can hold up to MAX_ALBUMS entries).
_album_lists = {}
_last_query = {}

_SEP = r"\s*[-\u2013\u2014:|]\s+"


def split_query(text):
    """'Artist - Song - Album' -> (artist, song, album).

    Missing pieces come back as "". Accepts '-', ':', '|' and en/em dashes,
    and the "Song by Artist" form.
    """
    raw = (text or "").strip()
    if not raw:
        return "", "", ""

    pieces = [p for p in (main.strip_noise(p)
                          for p in re.split(_SEP, raw, maxsplit=2)) if p]

    if len(pieces) >= 3:
        return pieces[0], pieces[1], pieces[2]
    if len(pieces) == 2:
        return pieces[0], pieces[1], ""

    flat = main.strip_noise(raw)
    m = re.match(r"^(.*?)\s+by\s+(.*)$", flat, flags=re.I)
    if m and m.group(1).strip() and m.group(2).strip():
        return m.group(2).strip(), m.group(1).strip(), ""
    return "", flat, ""


def album_score(query_album, album):
    """How well an album name matches. Roughly -16 (no match) .. +16 (exact)."""
    if not query_album:
        return 0.0
    if not album:
        return -8.0
    try:
        ratio = main.fuzz.token_set_ratio(query_album.lower(), album.lower()) / 100.0
    except Exception:
        ratio = 1.0 if query_album.lower() in album.lower() else 0.0
    return (ratio - 0.6) * 40.0


def rank_rows(query_artist, query_song, query_album, rows):
    """Score and de-duplicate rows, keeping one entry PER ALBUM."""
    scored, seen = [], set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        artist = (row.get('artistName') or '').strip()
        song = (row.get('trackName') or '').strip()
        album = (row.get('albumName') or '').strip()
        if not song:
            continue

        # one line per distinct album, so "all albums" really means all albums
        key = (main.normalize_str(artist), main.normalize_str(song),
               main.normalize_str(album))
        if key in seen:
            continue
        seen.add(key)

        score = main.score_track_match(query_artist, query_song, artist, song)
        if not query_artist:
            # the query was a single string: it may be the song, or "artist song"
            score = max(score,
                        main.score_track_match('', query_song, artist, song),
                        main.score_track_match(artist, query_song, artist, song) * 0.9)

        if query_album:
            boost = album_score(query_album, album)
            score += boost
            if boost < -5:
                score -= 25               # clearly a different album

        if row.get('syncedLyrics'):
            score += 6
        elif row.get('plainLyrics'):
            score += 3
        if re.search(r"\b(instrumental|karaoke|tribute|8d audio|lullaby)\b",
                     song, re.I):
            score -= 12

        scored.append((score, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


# ------------------------------------------------------------- searching
def search_tracks(query, limit=8):
    """lrclib-only search. No iTunes, ever.

    Steps:
      1. /api/get  - exact lookup, album included when the user named one
      2. /api/search - one ordinary search for everything else
    """
    cleaned = main.clean_query_text(query)
    if not cleaned:
        return []

    cache_key = "album|" + main.normalize_str(cleaned)
    try:
        hit = main.search_cache.get(cache_key)
        if hit and (time.time() - hit[0]) < main.SEARCH_CACHE_TTL:
            return hit[1][:limit]
    except Exception:
        pass

    q_artist, q_song, q_album = split_query(cleaned)
    rows = []

    # 1) precise lookup (lrclib /api/get understands album_name)
    if q_song:
        params = {'track_name': q_song[:120]}
        if q_artist:
            params['artist_name'] = q_artist[:120]
        if q_album:
            params['album_name'] = q_album[:120]
        exact = main.lrclib_get("https://lrclib.net/api/get",
                                params=params, timeout=8)
        if isinstance(exact, dict) and exact.get('id'):
            rows.append(exact)

    # 2) one ordinary search - the only other request we make
    candidate = " ".join(p for p in (q_artist, q_song, q_album) if p) or cleaned
    found = main.lrclib_get("https://lrclib.net/api/search",
                            params={'q': candidate[:120]}, timeout=8)
    if isinstance(found, list):
        rows.extend(found)

    ranked = rank_rows(q_artist, q_song, q_album, rows)
    main.remember_tracks([row for _, row in ranked])

    result = ranked[:max(limit, MAX_ALBUMS)]
    try:
        main.search_cache[cache_key] = (time.time(), result)
    except Exception:
        pass
    return result[:limit]


try:
    main.search_tracks = search_tracks
    print("[INFO] search_fix: album-aware search installed (iTunes disabled)")
except Exception as e:
    print(f"[ERROR] search_fix: could not install search_tracks: {e}")


# -------------------------------------------------------------- rendering
def fmt_duration(seconds):
    try:
        seconds = int(seconds or 0)
    except Exception:
        return ""
    if seconds <= 0:
        return ""
    return f"{seconds // 60}:{seconds % 60:02d}"


def track_line(track, maxlen=56):
    """'Adele - Hello - 4:55 - 25' style one-liner.

    The duration comes BEFORE the album so a long album name can never push
    the duration off the end of the (truncated) button label.
    """
    artist = (track.get('artistName') or 'Unknown').strip()
    song = (track.get('trackName') or 'Unknown').strip()
    album = (track.get('albumName') or '').strip()
    dur = fmt_duration(track.get('duration'))

    parts = [f"{artist} - {song}"]
    if dur:
        parts.append(dur)
    if album:
        parts.append(album)
    line = " - ".join(parts)
    return line if len(line) <= maxlen else line[:maxlen - 1] + "\u2026"


def track_header(track):
    """Album + duration block used on the card and above the lyrics."""
    artist = (track.get('artistName') or 'Unknown').strip()
    song = (track.get('trackName') or 'Unknown').strip()
    album = (track.get('albumName') or '').strip()
    dur = fmt_duration(track.get('duration'))

    lines = [f"\U0001f3b5 {artist}", f"\U0001f4dd {song}",
             f"\U0001f4bf {album or 'Unknown album'}",
             f"\u23f1 {dur or 'unknown length'}"]
    return "\n".join(lines)


def card_markup(track, back=True):
    tid = track.get('id')
    markup = types.InlineKeyboardMarkup(row_width=2)
    if track.get('syncedLyrics'):
        markup.add(
            types.InlineKeyboardButton("\U0001f3bc Synced \u2192 Text",
                                       callback_data=f"st_{tid}"),
            types.InlineKeyboardButton("\U0001f3bc Synced \u2192 .lrc",
                                       callback_data=f"sf_{tid}"),
        )
    if track.get('plainLyrics'):
        markup.add(
            types.InlineKeyboardButton("\U0001f4c4 Plain \u2192 Text",
                                       callback_data=f"pt_{tid}"),
            types.InlineKeyboardButton("\U0001f4c4 Plain \u2192 .txt",
                                       callback_data=f"pf_{tid}"),
        )
    if back:
        markup.add(types.InlineKeyboardButton("\u2b05\ufe0f Back to albums",
                                              callback_data="albums"))
    return markup


def album_list_text(ranked, query):
    q_artist, q_song, q_album = split_query(main.clean_query_text(query))
    n = len(ranked)
    head = (f"\U0001f3b7 {n} album version{'s' if n != 1 else ''} of "
            f"\u201c{q_song or query}\u201d")
    if q_artist:
        head += f" by {q_artist}"
    return head + " - pick one:"


def album_list_markup(ranked):
    markup = types.InlineKeyboardMarkup()
    for _score, track in ranked[:MAX_ALBUMS]:
        markup.add(types.InlineKeyboardButton(
            track_line(track), callback_data=f"pick_{track.get('id')}"))
    return markup


# ------------------------------------------------------- tap-to-copy lyrics
def code_block(text):
    """Telegram code block = one tap to copy. Backslashes/backticks escaped."""
    safe = str(text or "").replace("\\", "\\\\").replace("`", "\\`")
    return f"```\n{safe}\n```"


def send_copyable(chat_id, text, reply_to=None, header=None):
    """Send lyrics as a copyable block; only the first bubble is a reply."""
    text = (text or "").strip()
    if not text:
        return
    head = (header + "\n\n") if header else ""
    limit = 3200

    if len(text) <= limit:
        _send_block(chat_id, head + code_block(text), text, reply_to)
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
        prefix = head if i == 0 else ""
        target = reply_to if i == 0 else None
        _send_block(chat_id, prefix + code_block(chunk.strip()), chunk.strip(),
                    target)


def _send_block(chat_id, markdown_body, plain_body, reply_to):
    try:
        bot.send_message(chat_id, markdown_body, parse_mode='Markdown',
                         reply_to_message_id=reply_to)
    except Exception as e:
        print(f"[DEBUG] code block failed, plain fallback: {e}")
        bot.send_message(chat_id,
                         (markdown_body.replace("```", "") if markdown_body
                          else plain_body),
                         reply_to_message_id=reply_to)


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

    try:
        ranked = search_tracks(query, limit=MAX_ALBUMS)
    except Exception as e:
        print(f"[ERROR] search_fix: search crashed: {e}")
        ranked = []

    _q_artist, _q_song, q_album = split_query(main.clean_query_text(query))

    if not ranked:
        try:
            # split_query understands the third (album) field, so
            # "Adele - Hello - 25" does not become the song "Hello - 25"
            artist_part, title_part = split_query(main.clean_query_text(query))[:2]
            if not title_part:
                title_part, artist_part = (query or "").strip(), ""
            backup = fetch_lyrics_backup(artist_part, title_part)
        except Exception as e:
            print(f"[DEBUG] backup lookup error: {e}")
            backup = None

        if backup:
            track = make_backup_track(artist_part, title_part, backup)
            report(message, status,
                   "\u2139\ufe0f Found the lyrics on the backup source.\n\n"
                   + track_header(track),
                   markup=card_markup(track, back=False))
            return

        why = last_lrclib_status()
        if why in (None, "ok", "not found"):
            report(message, status,
                   "\U0001f937 No lyrics found for that.\n\n"
                   "Try:  Artist - Song - Album\n"
                   "e.g.  Adele - Hello - 25")
        elif why and "429" in str(why):
            report(message, status,
                   "\u23f3 The lyrics service is rate-limiting us right now.\n\n"
                   "Please wait about 30 seconds and search again.")
        else:
            report(message, status,
                   f"\u26a0\ufe0f The lyrics service isn't answering right now ({why}).\n\n"
                   "Please try again in a minute.")
        return

    try:
        main.remember_results(chat_id, ranked)
    except Exception:
        pass
    try:
        _album_lists[chat_id] = ranked[:MAX_ALBUMS]
        _last_query[chat_id] = query
        if len(_album_lists) > 200:
            _album_lists.pop(next(iter(_album_lists)), None)
    except Exception:
        pass

    best_score, best = ranked[0]
    has_lyrics = best.get('plainLyrics') or best.get('syncedLyrics')

    # An album was named and we found a solid match -> show that one directly.
    if q_album and best_score >= 70 and has_lyrics:
        report(message, status, track_header(best),
               markup=card_markup(best, back=True))
        return

    # Only one album version exists -> show it.
    if len(ranked) == 1 and has_lyrics:
        report(message, status, track_header(best),
               markup=card_markup(best, back=False))
        return

    # Otherwise list every album version we found.
    report(message, status, album_list_text(ranked, query),
           markup=album_list_markup(ranked))


# ------------------------------------------- fallback lyrics source (no key)
def fetch_lyrics_backup(artist, title):
    """Plain lyrics from lyrics.ovh - used only when lrclib gives us nothing."""
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
        main.track_cache[track_id] = track
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
        ranked = search_tracks(f"{artist} - {title}".strip(), limit=2)
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

    best = ranked[0][1]
    report(message, status, track_header(best),
           markup=card_markup(best, back=True))


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
        bot.reply_to(message, "Usage:\n/lyrics Artist - Song - Album\n/find a line from the song")
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
                              "!song Adele - Hello - 25\n"
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
    original = admin_db.is_admin
    admin_db.is_admin = lambda user_id: True
    try:
        main.handle_music_file(message)
    finally:
        admin_db.is_admin = original


_promote(bot.message_handlers)


@bot.callback_query_handler(func=lambda call: call.data.startswith('pick_'))
def _pick_album(call):
    """User chose one album from the list."""
    track_id = call.data.split('_', 1)[1]
    track = main.get_lyrics_by_track_id(track_id)
    if not track:
        try:
            bot.answer_callback_query(call.id, "Could not load that album - search again.")
        except Exception:
            pass
        return
    try:
        bot.edit_message_text(track_header(track), call.message.chat.id,
                              call.message.message_id,
                              reply_markup=card_markup(track, back=True))
    except Exception as e:
        print(f"[ERROR] search_fix: pick failed: {e}")
        try:
            bot.send_message(call.message.chat.id, track_header(track),
                             reply_markup=card_markup(track, back=True))
        except Exception:
            pass
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass


_promote(bot.callback_query_handlers)


@bot.callback_query_handler(func=lambda call: call.data == 'albums')
def _back_to_albums(call):
    """Back to the album list for the chat's last search."""
    ranked = _album_lists.get(call.message.chat.id)
    if not ranked:
        try:
            bot.answer_callback_query(call.id, "Search again to see the albums.")
        except Exception:
            pass
        return
    try:
        bot.edit_message_text(
            album_list_text(ranked, _last_query.get(call.message.chat.id, "")),
            call.message.chat.id, call.message.message_id,
            reply_markup=album_list_markup(ranked))
    except Exception as e:
        print(f"[ERROR] search_fix: back failed: {e}")
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass


_promote(bot.callback_query_handlers)


@bot.callback_query_handler(func=lambda call: call.data.startswith(('sf_', 'st_', 'pf_', 'pt_')))
def _deliver_lyrics_copyable(call):
    """Send the lyrics as a tap-to-copy block, with album + duration on top."""
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
    album = (track.get('albumName') or 'Unknown album').strip()[:28]
    chat_id = call.message.chat.id

    # reply to the message the user originally searched with
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
            try:
                bot.send_document(chat_id, bio, reply_to_message_id=reply_to,
                                  caption=track_header(track))
            except Exception:
                bot.send_document(chat_id, bio, reply_to_message_id=reply_to)
            del bio
        else:
            data = track.get('syncedLyrics') if cmd == 'st' else track.get('plainLyrics')
            if not data:
                bot.answer_callback_query(
                    call.id,
                    "No synced lyrics for this one." if cmd == 'st' else "No plain lyrics for this one.")
                return
            send_copyable(chat_id, data, reply_to, header=track_header(track))
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

print("[INFO] search_fix loaded: Artist - Song - Album search, albums + durations everywhere")
