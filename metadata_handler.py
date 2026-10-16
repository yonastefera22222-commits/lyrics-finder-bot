# metadata_handler.py
import os
import logging
import tempfile
import traceback
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, USLT, COMM, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4, MP4Tags
from mutagen.oggvorbis import OggVorbis

logger = logging.getLogger("metadata_handler")
handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)


def sanitize_text(value):
    if value is None:
        return None
    try:
        return str(value).strip()
    except Exception:
        return None


def read_id3v2_metadata(file_path):
    """Extract metadata (artist/title/album/duration) from common audio files.
    Returns dict or None on error.
    """
    try:
        if not os.path.exists(file_path):
            logger.error("file not found: %s", file_path)
            return None

        audio = MutagenFile(file_path, easy=False)
        if audio is None:
            logger.debug("mutagen could not identify file: %s", file_path)
            return None

        artist = None
        title = None
        album = None
        duration = None

        # Duration if available
        try:
            duration = int(audio.info.length) if getattr(audio, 'info', None) and getattr(audio.info, 'length', None) else None
        except Exception:
            duration = None

        # MP3 ID3
        if isinstance(audio, MP3) or file_path.lower().endswith('.mp3'):
            try:
                id3 = ID3(file_path)
                artist = sanitize_text(id3.get('TPE1').text[0]) if id3.get('TPE1') else None
                title = sanitize_text(id3.get('TIT2').text[0]) if id3.get('TIT2') else None
                album = sanitize_text(id3.get('TALB').text[0]) if id3.get('TALB') else None
            except ID3NoHeaderError:
                # no ID3 tags
                logger.debug("No ID3 header in MP3: %s", file_path)
            except Exception:
                logger.exception("Error reading ID3 tags")

        # MP4/M4A
        elif isinstance(audio, MP4) or file_path.lower().endswith(('.m4a', '.mp4', '.m4b')):
            try:
                mp4 = MP4(file_path)
                # ©ART, ©nam, ©alb
                artist = sanitize_text(mp4.tags.get('\u00a9ART', [None])[0]) if mp4.tags else artist
                title = sanitize_text(mp4.tags.get('\u00a9nam', [None])[0]) if mp4.tags else title
                album = sanitize_text(mp4.tags.get('\u00a9alb', [None])[0]) if mp4.tags else album
            except Exception:
                logger.exception("Error reading MP4 tags")

        # FLAC
        elif isinstance(audio, FLAC) or file_path.lower().endswith('.flac'):
            try:
                fl = FLAC(file_path)
                artist = sanitize_text(fl.get('artist', [None])[0])
                title = sanitize_text(fl.get('title', [None])[0])
                album = sanitize_text(fl.get('album', [None])[0])
            except Exception:
                logger.exception("Error reading FLAC tags")

        # Ogg Vorbis / Opus
        elif isinstance(audio, OggVorbis) or file_path.lower().endswith(('.ogg', '.opus')):
            try:
                og = OggVorbis(file_path)
                artist = sanitize_text(og.get('artist', [None])[0])
                title = sanitize_text(og.get('title', [None])[0])
                album = sanitize_text(og.get('album', [None])[0])
            except Exception:
                logger.exception("Error reading Ogg tags")

        # Fallback: inspect common tags
        if not artist or not title:
            try:
                tags = getattr(audio, 'tags', None)
                if tags:
                    if not artist:
                        artist = sanitize_text(tags.get('artist', [None])[0] if isinstance(tags.get('artist', None), list) else tags.get('artist'))
                    if not title:
                        title = sanitize_text(tags.get('title', [None])[0] if isinstance(tags.get('title', None), list) else tags.get('title'))
                    if not album:
                        album = sanitize_text(tags.get('album', [None])[0] if isinstance(tags.get('album', None), list) else tags.get('album'))
            except Exception:
                logger.debug("fallback tag read failed")

        # Last resort: filename
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
            'artist': (artist or "Unknown Artist")[:200],
            'title': (title or "Unknown Track")[:200],
            'album': (album or "Unknown Album")[:200],
            'duration': duration
        }

        logger.debug("Metadata extracted: %s - %s", metadata['artist'], metadata['title'])
        return metadata

    except Exception as e:
        logger.exception("Metadata extraction error: %s", e)
        return None


def embed_lyrics_in_file(file_path, lyrics_text, lyrics_type="plain"):
    """Embed lyrics into supported audio files.
    lyrics_type: 'plain' or 'synced'
    Returns True on success, False otherwise.
    """
    try:
        if not os.path.exists(file_path):
            logger.error("file not found for embedding: %s", file_path)
            return False

        if not lyrics_text or not lyrics_text.strip():
            logger.error("empty lyrics provided")
            return False

        # Limit size to avoid excessive tag sizes (configurable)
        max_bytes = 200 * 1024  # increased to 200KB to allow longer lyrics (adjustable)
        raw = lyrics_text
        encoded = raw.encode('utf-8')
        if len(encoded) > max_bytes:
            logger.debug("lyrics too large (%d bytes), truncating to %d bytes", len(encoded), max_bytes)
            # truncate safely on UTF-8 char boundary
            encoded = encoded[:max_bytes]
            # drop any partial trailing multi-byte sequence
            while True:
                try:
                    raw = encoded.decode('utf-8')
                    break
                except UnicodeDecodeError:
                    encoded = encoded[:-1]
                    if not encoded:
                        raw = ""
                        break
            lyrics_text = raw

        audio = MutagenFile(file_path, easy=False)
        if audio is None:
            logger.error("unsupported or unreadable file for embedding: %s", file_path)
            return False

        # MP3 -> ID3 USLT for plain. For synced LRC, store as COMM with desc 'synced'
        if isinstance(audio, MP3) or file_path.lower().endswith('.mp3'):
            try:
                try:
                    id3 = ID3(file_path)
                except ID3NoHeaderError:
                    logger.debug("No ID3 header, creating one")
                    id3 = ID3()

                if lyrics_type == 'plain':
                    # Remove existing USLT frames (keep other frames intact)
                    try:
                        id3.delall('USLT')
                    except Exception:
                        pass
                    uslt = USLT(encoding=3, lang='eng', desc='', text=lyrics_text)
                    id3.add(uslt)
                else:
                    # synced: remove only COMM frames with desc == 'synced', preserve others
                    existing_comm = id3.getall('COMM')
                    preserved = [f for f in existing_comm if getattr(f, 'desc', '') != 'synced']
                    # clear all COMM frames then re-add preserved ones
                    try:
                        id3.delall('COMM')
                    except Exception:
                        pass
                    for f in preserved:
                        id3.add(f)
                    comm = COMM(encoding=3, lang='eng', desc='synced', text=[lyrics_text])
                    id3.add(comm)

                # Save tags back to file
                id3.save(file_path, v2_version=3, padding=2048)
            except Exception:
                logger.exception("Failed to write ID3 tags")
                return False

            # verify presence
            try:
                id3_check = ID3(file_path)
                if lyrics_type == 'plain':
                    uslt_frames = id3_check.getall('USLT')
                    if uslt_frames:
                        logger.debug("USLT present after save")
                        return True
                    else:
                        logger.error("USLT not found after save")
                        return False
                else:
                    comm_frames = id3_check.getall('COMM')
                    # check for a COMM frame with desc 'synced'
                    found = any(getattr(c, 'desc', '') == 'synced' for c in comm_frames)
                    if found:
                        logger.debug("COMM synced present after save")
                        return True
                    else:
                        logger.error("COMM (synced) not found after save")
                        return False
            except Exception:
                logger.exception("Verification exception for MP3")
                return False

        # MP4/M4A: MP4Tags '©lyr'
        elif isinstance(audio, MP4) or file_path.lower().endswith(('.m4a', '.mp4', '.m4b')):
            try:
                mp4 = MP4(file_path)
                tags = mp4.tags or MP4Tags()
                key = '\u00a9lyr'
                # MP4 tags expect list of strings
                tags[key] = [lyrics_text]
                mp4.tags = tags
                mp4.save()
            except Exception:
                logger.exception("Failed to write MP4 tags")
                return False

            # verify
            try:
                mp4_check = MP4(file_path)
                if mp4_check.tags and mp4_check.tags.get('\u00a9lyr'):
                    logger.debug("MP4 ©lyr present after save")
                    return True
                logger.error("Verification failed for MP4 embedding")
                return False
            except Exception:
                logger.exception("Verification exception for MP4")
                return False

        # FLAC
        elif isinstance(audio, FLAC) or file_path.lower().endswith('.flac'):
            try:
                fl = FLAC(file_path)
                # Vorbis-style tags expect lists
                if lyrics_type == 'plain':
                    fl['LYRICS'] = [lyrics_text]
                else:
                    fl['SYNCEDLYRICS'] = [lyrics_text]
                fl.save()
            except Exception:
                logger.exception("Failed to write FLAC tags")
                return False

            # verify
            try:
                fl_check = FLAC(file_path)
                if lyrics_type == 'plain' and fl_check.get('LYRICS'):
                    logger.debug("FLAC LYRICS present after save")
                    return True
                if lyrics_type == 'synced' and fl_check.get('SYNCEDLYRICS'):
                    logger.debug("FLAC SYNCEDLYRICS present after save")
                    return True
                logger.error("Verification failed for FLAC embedding")
                return False
            except Exception:
                logger.exception("Verification exception for FLAC")
                return False

        # OGG/Opus
        elif isinstance(audio, OggVorbis) or file_path.lower().endswith(('.ogg', '.opus')):
            try:
                og = OggVorbis(file_path)
                if lyrics_type == 'plain':
                    og['LYRICS'] = [lyrics_text]
                else:
                    og['SYNCEDLYRICS'] = [lyrics_text]
                og.save()
            except Exception:
                logger.exception("Failed to write Ogg tags")
                return False

            # verify
            try:
                og_check = OggVorbis(file_path)
                if lyrics_type == 'plain' and og_check.get('LYRICS'):
                    logger.debug("OGG LYRICS present after save")
                    return True
                if lyrics_type == 'synced' and og_check.get('SYNCEDLYRICS'):
                    logger.debug("OGG SYNCEDLYRICS present after save")
                    return True
                logger.error("Verification failed for OGG")
                return False
            except Exception:
                logger.exception("Verification exception for OGG")
                return False

        else:
            logger.error("Unsupported audio container for embedding: %s", file_path)
            return False

    except Exception as e:
        logger.exception("Embed error: %s", e)
        return False
