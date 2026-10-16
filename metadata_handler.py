# metadata_handler.py
"""
Audio metadata reading + lyrics embedding.

Read support   : mp3, m4a/mp4/m4b (AAC/ALAC), flac, ogg vorbis (.ogg/.oga),
                 ogg opus (.opus/.oga), wav, aiff/aif
Embed support  : mp3 (ID3v2.3  USLT), wav + aiff (ID3), flac (LYRICS),
                 ogg vorbis / ogg opus (LYRICS), m4a/mp4/m4b (©lyr)

Notes for compressed/ephemeral hosts (Render free tier, etc.):
  * Tagging is done on a temporary copy first and only swapped in when the
    verification step succeeds, so a failed embed can never corrupt the
    user's uploaded file.
  * All tag sizes are capped to keep memory/disk usage low.
"""
import os
import shutil
import logging
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, USLT, COMM, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4, MP4Tags
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus
from mutagen.wave import WAVE
from mutagen.aiff import AIFF

logger = logging.getLogger("metadata_handler")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.DEBUG)

# Bump this whenever the file changes: the bot prints it on startup and the
# /diag command shows it, so it is obvious whether a deploy really took the
# new file or still runs an old copy.
CODE_VERSION = "1.3"

# Human readable reason for the last failed embed (shown in /diag and in the
# chat when embedding fails).
LAST_ERROR = None

def _fail(reason):
    global LAST_ERROR
    LAST_ERROR = reason
    logger.error("embed failed: %s", reason)
    return False

# --- Limits / constants -----------------------------------------------------
MAX_LYRICS_BYTES = int(os.environ.get("MAX_LYRICS_BYTES", 200 * 1024))  # 200 KB
# ID3v2.3 does not officially support UTF-8 -> use UTF-16 (encoding=1) so that
# car stereos / older players read the lyrics correctly.
ID3_TEXT_ENCODING = 1

MP4_LYRICS_KEY = "\u00a9lyr"  # (c)lyr atom

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".mp4", ".m4b", ".mov", ".flac",
    ".ogg", ".oga", ".opus", ".wav", ".aiff", ".aif",
}

VORBIS_EXTS = {".ogg", ".oga", ".opus"}
MP4_EXTS = {".m4a", ".mp4", ".m4b", ".mov"}
FLAC_EXTS = {".flac"}
ID3_EXTS = {".mp3", ".wav", ".aiff", ".aif"}

def sanitize_text(value):
    if value is None:
        return None
    try:
        return str(value).strip()
    except Exception:
        return None

def get_file_ext(file_path):
    return os.path.splitext(str(file_path))[1].lower()

# --- helpers ---------------------------------------------------------------
def _id3_padding(info):
    """mutagen expects a *callable* padding function, not an int.

    Passing an int (e.g. ``padding=2048``) raises
    ``TypeError: 'int' object is not callable`` while saving, which made every
    MP3 embed fail. This keeps a small padding block so later tag edits are
    fast without bloating the file.
    """
    try:
        return 2048
    except Exception:
        return 0

def _utf8_truncate(text, max_bytes=MAX_LYRICS_BYTES):
    """Truncate text on a UTF-8 character boundary."""
    try:
        encoded = text.encode("utf-8")
    except Exception:
        return text
    if len(encoded) <= max_bytes:
        return text
    encoded = encoded[:max_bytes]
    while True:
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError:
            encoded = encoded[:-1]
            if not encoded:
                return ""

def _id3_first_text(id3, key):
    """Safely read the first text value of an ID3 frame."""
    try:
        frame = id3.get(key)
        if frame is None:
            return None
        text = getattr(frame, "text", None)
        if isinstance(text, (list, tuple)):
            return sanitize_text(text[0]) if len(text) else None
        if text is None:
            return None
        return sanitize_text(text)
    except Exception:
        return None

def _vorbis_first_str(obj, key):
    try:
        value = obj.get(key)
        if not value:
            return None
        if isinstance(value, (list, tuple)):
            return sanitize_text(value[0]) if len(value) else None
        return sanitize_text(value)
    except Exception:
        return None

def _mp4_first_str(tags, key):
    try:
        if not tags:
            return None
        value = tags.get(key)
        if not value:
            return None
        if isinstance(value, (list, tuple)):
            return sanitize_text(value[0]) if len(value) else None
        return sanitize_text(value)
    except Exception:
        return None

# --- reading ---------------------------------------------------------------
def read_id3v2_metadata(file_path):
    """Extract metadata (artist/title/album/duration) from common audio files.

    Returns a dict, or None when the file cannot be read at all.
    """
    try:
        if not file_path or not os.path.exists(file_path):
            logger.error("file not found: %s", file_path)
            return None

        ext = get_file_ext(file_path)

        audio = None
        try:
            audio = MutagenFile(file_path, easy=False)
        except Exception:
            logger.debug("mutagen could not identify file: %s", file_path, exc_info=True)

        artist = title = album = None
        duration = None

        if audio is not None and getattr(audio, "info", None) is not None:
            try:
                length = getattr(audio.info, "length", None)
                duration = int(length) if length else None
            except Exception:
                duration = None

        # ---- MP3 (ID3) -----------------------------------------------------
        if isinstance(audio, MP3) or ext == ".mp3":
            try:
                id3 = ID3(file_path)
                artist = _id3_first_text(id3, "TPE1")
                title = _id3_first_text(id3, "TIT2")
                album = _id3_first_text(id3, "TALB")
            except ID3NoHeaderError:
                logger.debug("No ID3 header in MP3: %s", file_path)
            except Exception:
                logger.exception("Error reading ID3 tags")

        # ---- MP4 / M4A / M4B (also .mp4 video containers) ------------------
        elif isinstance(audio, MP4) or ext in MP4_EXTS:
            try:
                mp4 = MP4(file_path)
                tags = mp4.tags
                artist = _mp4_first_str(tags, "\u00a9ART")
                title = _mp4_first_str(tags, "\u00a9nam")
                album = _mp4_first_str(tags, "\u00a9alb")
            except Exception:
                logger.exception("Error reading MP4 tags")

        # ---- FLAC ----------------------------------------------------------
        elif isinstance(audio, FLAC) or ext in FLAC_EXTS:
            try:
                fl = FLAC(file_path)
                artist = _vorbis_first_str(fl, "artist")
                title = _vorbis_first_str(fl, "title")
                album = _vorbis_first_str(fl, "album")
            except Exception:
                logger.exception("Error reading FLAC tags")

        # ---- Ogg Opus (must be checked before Vorbis, .oga can be either) --
        elif isinstance(audio, OggOpus) or ext == ".opus":
            try:
                og = OggOpus(file_path)
                artist = _vorbis_first_str(og, "artist")
                title = _vorbis_first_str(og, "title")
                album = _vorbis_first_str(og, "album")
            except Exception:
                logger.exception("Error reading Ogg Opus tags")

        # ---- Ogg Vorbis (.ogg / .oga) --------------------------------------
        elif isinstance(audio, OggVorbis) or ext in VORBIS_EXTS:
            try:
                og = OggVorbis(file_path)
                artist = _vorbis_first_str(og, "artist")
                title = _vorbis_first_str(og, "title")
                album = _vorbis_first_str(og, "album")
            except Exception:
                logger.exception("Error reading Ogg Vorbis tags")

        # ---- WAV -----------------------------------------------------------
        elif isinstance(audio, WAVE) or ext == ".wav":
            try:
                wav = WAVE(file_path)
                tags = getattr(wav, "tags", None)
                if tags is not None:
                    artist = _id3_first_text(tags, "TPE1")
                    title = _id3_first_text(tags, "TIT2")
                    album = _id3_first_text(tags, "TALB")
            except Exception:
                logger.exception("Error reading WAV tags")

        # ---- AIFF ----------------------------------------------------------
        elif isinstance(audio, AIFF) or ext in (".aiff", ".aif"):
            try:
                aif = AIFF(file_path)
                tags = getattr(aif, "tags", None)
                if tags is not None:
                    artist = _id3_first_text(tags, "TPE1")
                    title = _id3_first_text(tags, "TIT2")
                    album = _id3_first_text(tags, "TALB")
            except Exception:
                logger.exception("Error reading AIFF tags")

        # ---- Generic fallback: any file that carries an ID3 header ---------
        if not artist or not title:
            try:
                id3 = ID3(file_path)
                artist = artist or _id3_first_text(id3, "TPE1")
                title = title or _id3_first_text(id3, "TIT2")
                album = album or _id3_first_text(id3, "TALB")
            except Exception:
                pass

        # ---- Generic fallback: vorbis-style tags ---------------------------
        if (not artist or not title) and audio is not None:
            try:
                tags = getattr(audio, "tags", None)
                if tags is not None and hasattr(tags, "get"):
                    artist = artist or _vorbis_first_str(tags, "artist")
                    title = title or _vorbis_first_str(tags, "title")
                    album = album or _vorbis_first_str(tags, "album")
            except Exception:
                logger.debug("fallback tag read failed", exc_info=True)

        # ---- Last resort: filename ----------------------------------------
        if not title:
            try:
                title = os.path.splitext(os.path.basename(file_path))[0]
            except Exception:
                title = "Unknown Track"
        if not artist:
            artist = "Unknown Artist"
        if not album:
            album = "Unknown Album"

        metadata = {
            "artist": (artist or "Unknown Artist")[:200],
            "title": (title or "Unknown Track")[:200],
            "album": (album or "Unknown Album")[:200],
            "duration": duration,
            "has_real_tags": bool(
                artist and artist != "Unknown Artist"
                and title and title != os.path.splitext(os.path.basename(file_path))[0]
            ),
        }

        logger.debug("Metadata extracted: %s - %s", metadata["artist"], metadata["title"])
        return metadata

    except Exception as e:
        logger.exception("Metadata extraction error: %s", e)
        return None

# --- embedding -------------------------------------------------------------
def _write_id3_lyrics(id3, lyrics_text, lyrics_type):
    """Add lyrics frames to an ID3 tag object (mp3 / wav / aiff)."""
    if lyrics_type == "synced":
        # Keep LRC timestamps in USLT as well: that is what most players
        # (Poweramp, Musicolet, VLC, Samsung Music, ...) actually display.
        try:
            id3.delall("USLT")
        except Exception:
            pass
        id3.add(USLT(encoding=ID3_TEXT_ENCODING, lang="eng", desc="", text=lyrics_text))

        # keep every other COMM frame, replace only our marker
        preserved = []
        try:
            preserved = [f for f in id3.getall("COMM") if getattr(f, "desc", "") != "synced"]
            id3.delall("COMM")
        except Exception:
            preserved = []
        for frame in preserved:
            id3.add(frame)
        id3.add(COMM(encoding=ID3_TEXT_ENCODING, lang="eng", desc="synced", text=[lyrics_text]))
    else:
        try:
            id3.delall("USLT")
        except Exception:
            pass
        id3.add(USLT(encoding=ID3_TEXT_ENCODING, lang="eng", desc="", text=lyrics_text))
        # drop a stale "synced" marker so the file is honestly marked as plain
        try:
            preserved = [f for f in id3.getall("COMM") if getattr(f, "desc", "") != "synced"]
            id3.delall("COMM")
            for frame in preserved:
                id3.add(frame)
        except Exception:
            pass

def _embed_into(file_path, lyrics_text, lyrics_type, ext=None, clean=False):
    """Tag one (already copied) file. Returns True/False.

    ``ext`` is the extension of the *original* upload. It is passed in so a
    temporary file name can never change how the format is detected.
    ``clean`` throws away every non essential tag frame first, which rescues
    files whose existing tags cannot be rewritten.
    """
    ext = (ext or "").lower() or get_file_ext(file_path)

    try:
        audio = MutagenFile(file_path, easy=False)
    except Exception:
        audio = None

    # ---------------- MP3 + WAV + AIFF (ID3) --------------------------------
    if isinstance(audio, MP3) or ext in ID3_EXTS:
        try:
            if isinstance(audio, WAVE) or ext == ".wav":
                container = WAVE(file_path)
            elif isinstance(audio, AIFF) or ext in (".aiff", ".aif"):
                container = AIFF(file_path)
            else:
                container = None

            if container is not None:
                if getattr(container, "tags", None) is None:
                    container.add_tags()
                _write_id3_lyrics(container.tags, lyrics_text, lyrics_type)
                container.save()
                return _verify_id3(file_path, lyrics_type)
            else:
                try:
                    id3 = ID3(file_path)
                except ID3NoHeaderError:
                    logger.debug("No ID3 header, creating one")
                    id3 = ID3()
                if clean:
                    keep = {}
                    for key in ("TIT2", "TPE1", "TALB", "TRCK", "TLEN"):
                        if key in id3:
                            keep[key] = id3[key]
                    try:
                        id3.delete()
                    except Exception:
                        pass
                    for key, frame in keep.items():
                        try:
                            id3.add(frame)
                        except Exception:
                            pass
                _write_id3_lyrics(id3, lyrics_text, lyrics_type)
                # NOTE: padding must be a callable for mutagen >= 1.42
                id3.save(file_path, v2_version=3, padding=_id3_padding)
                return _verify_id3(file_path, lyrics_type)
        except Exception:
            logger.exception("Failed to write ID3 tags")
            return False

    # ---------------- MP4 / M4A / M4B --------------------------------------
    if isinstance(audio, MP4) or ext in MP4_EXTS:
        try:
            mp4 = MP4(file_path)
            tags = mp4.tags or MP4Tags()
            tags[MP4_LYRICS_KEY] = [lyrics_text]
            mp4.tags = tags
            mp4.save()
        except Exception:
            logger.exception("Failed to write MP4 tags")
            return False
        try:
            check = MP4(file_path)
            value = _mp4_first_str(check.tags, MP4_LYRICS_KEY)
            if value:
                logger.debug("MP4 lyrics atom present after save")
                return True
            logger.error("Verification failed for MP4 embedding")
            return False
        except Exception:
            logger.exception("Verification exception for MP4")
            return False

    # ---------------- FLAC / OGG (vorbis comments) --------------------------
    if isinstance(audio, (FLAC, OggVorbis, OggOpus)) or ext in FLAC_EXTS | VORBIS_EXTS:
        open_fn = None
        if isinstance(audio, FLAC) or ext in FLAC_EXTS:
            open_fn = FLAC
        elif isinstance(audio, OggOpus) or ext == ".opus":
            open_fn = OggOpus
        else:
            open_fn = OggVorbis

        try:
            target = open_fn(file_path)
            # LYRICS is read by foobar2000 / VLC / MusicBee / most players.
            target["LYRICS"] = [lyrics_text]
            if lyrics_type == "synced":
                target["SYNCEDLYRICS"] = [lyrics_text]
            else:
                try:
                    if "SYNCEDLYRICS" in target:
                        del target["SYNCEDLYRICS"]
                except Exception:
                    pass
            target.save()
        except Exception:
            logger.exception("Failed to write %s tags", open_fn.__name__)
            return False

        try:
            check = open_fn(file_path)
            value = _vorbis_first_str(check, "LYRICS")
            if value:
                logger.debug("%s LYRICS present after save", open_fn.__name__)
                return True
            logger.error("Verification failed for %s embedding", open_fn.__name__)
            return False
        except Exception:
            logger.exception("Verification exception for %s", open_fn.__name__)
            return False

    logger.error("Unsupported audio container for embedding: %s", file_path)
    return False

def _load_id3_tags(file_path, container=None):
    """Return an ID3 tag object for mp3/wav/aiff files.

    WAV and AIFF carry the ID3 chunk *inside* the RIFF/AIFF container, so
    ``ID3(path)`` raises ID3NoHeaderError for them - the container must be
    opened instead.
    """
    ext = get_file_ext(file_path)
    if container is None:
        try:
            container = MutagenFile(file_path, easy=False)
        except Exception:
            container = None

    if isinstance(container, WAVE) or ext == ".wav":
        wav = container if isinstance(container, WAVE) else WAVE(file_path)
        return wav.tags
    if isinstance(container, AIFF) or ext in (".aiff", ".aif"):
        aif = container if isinstance(container, AIFF) else AIFF(file_path)
        return aif.tags
    return ID3(file_path)

def _verify_id3(file_path, lyrics_type):
    """Verify that lyrics were really written."""
    try:
        id3 = _load_id3_tags(file_path)
        if id3 is None:
            logger.error("No ID3 tag block found after save")
            return False
        uslt_frames = id3.getall("USLT")
        text = None
        for frame in uslt_frames:
            frame_text = getattr(frame, "text", None)
            if isinstance(frame_text, (list, tuple)) and len(frame_text):
                text = frame_text[0]
            elif isinstance(frame_text, str):
                text = frame_text
            if text:
                break

        if not text:
            logger.error("USLT not found after save")
            return False

        if lyrics_type == "synced":
            has_marker = any(
                getattr(c, "desc", "") == "synced" for c in id3.getall("COMM")
            )
            if not has_marker:
                logger.warning("USLT written but synced COMM marker missing")
        logger.debug("ID3 lyrics verified")
        return True
    except Exception:
        logger.exception("Verification exception for ID3 file")
        return False

def embed_lyrics_in_file(file_path, lyrics_text, lyrics_type="plain"):
    """Embed lyrics into a supported audio file.

    lyrics_type: 'plain' or 'synced'
    The file is tagged through a temporary copy, so a failure never leaves the
    original file half-written. Returns True on success, False otherwise.
    """
    global LAST_ERROR
    LAST_ERROR = None
    tmp_path = None
    try:
        if not file_path or not os.path.exists(file_path):
            return _fail(f"file not found: {file_path}")

        if not lyrics_text or not str(lyrics_text).strip():
            return _fail("empty lyrics provided")

        lyrics_text = _utf8_truncate(str(lyrics_text))
        lyrics_type = "synced" if str(lyrics_type).lower() == "synced" else "plain"

        # work on a copy: only swap it in when tagging + verification succeed.
        # The copy must keep the original extension, otherwise format
        # detection (which is partly extension based) fails.
        base, ext = os.path.splitext(file_path)
        tmp_path = f"{base}.tagging{ext}"
        try:
            shutil.copyfile(file_path, tmp_path)
        except Exception:
            logger.exception("Could not create tagging copy; tagging in place")
            tmp_path = None

        target = tmp_path or file_path
        ext = get_file_ext(file_path)

        # attempt 1: normal tagging
        ok = _embed_into(target, lyrics_text, lyrics_type, ext=ext)

        # attempt 2: strip the file's existing tag frames and try again. This
        # rescues uploads whose old tags cannot be re-written (unusual frames,
        # broken padding, damaged tag block, ...).
        if not ok and tmp_path and os.path.exists(tmp_path):
            logger.warning("first embed attempt failed (%s); retrying with a clean tag", LAST_ERROR)
            try:
                shutil.copyfile(file_path, tmp_path)
            except Exception:
                pass
            ok = _embed_into(tmp_path, lyrics_text, lyrics_type, ext=ext, clean=True)

        if ok and tmp_path:
            try:
                os.replace(tmp_path, file_path)
                tmp_path = None
                ok = True
            except Exception:
                logger.exception("Could not replace original file with tagged copy")
                ok = False

        if not ok:
            if LAST_ERROR is None:
                LAST_ERROR = "embedding could not be verified"
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

        return bool(ok)

    except Exception as e:
        logger.exception("Embed error: %s", e)
        if LAST_ERROR is None:
            LAST_ERROR = f"{type(e).__name__}: {e}"
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return False

# --- self test (used by the bot's /diag command) ---------------------------
def _make_silent_mp3(path, frames=8):
    """Write a tiny valid MPEG1 Layer III file (128 kbps, 44.1 kHz, silence)."""
    frame_len = 144 * 128000 // 44100  # 417 bytes
    header = bytes([0xFF, 0xFB, 0x90, 0x64])
    frame = header + bytes(frame_len - 4)
    with open(path, "wb") as f:
        for _ in range(frames):
            f.write(frame)

def _make_silent_wav(path, seconds=0.2, rate=8000):
    """Write a tiny valid 16-bit mono WAV file (silence)."""
    import struct
    n = int(rate * seconds)
    data = b"\x00\x00" * n
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE")
        f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
        f.write(b"data" + struct.pack("<I", len(data)) + data)

def self_test():
    """Embed lyrics into freshly generated files; used by /diag.

    Returns a list of (format, ok) tuples so the bot can show exactly which
    formats work in the environment that is actually running.
    """
    import tempfile
    results = []
    tmpdir = tempfile.mkdtemp(prefix="mh_selftest_")
    lyrics = "self test line 1\nself test line 2"
    try:
        mp3_path = os.path.join(tmpdir, "selftest.mp3")
        _make_silent_mp3(mp3_path)
        results.append(("mp3", embed_lyrics_in_file(mp3_path, lyrics, "plain")))

        wav_path = os.path.join(tmpdir, "selftest.wav")
        _make_silent_wav(wav_path)
        results.append(("wav", embed_lyrics_in_file(wav_path, lyrics, "plain")))

        try:
            mp3_check = ID3(mp3_path).getall("USLT")
        except Exception:
            mp3_check = []
        results.append(("mp3 verify", bool(mp3_check)))
    except Exception as e:
        logger.exception("self test error")
        results.append(("self test", False))
        global LAST_ERROR
        LAST_ERROR = f"{type(e).__name__}: {e}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return results
