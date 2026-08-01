import os
import re
import logging
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, USLT, ID3NoHeaderError, COMM
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4

logger = logging.getLogger(__name__)

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9 ._\-()]")


def _sanitize_text(s: str) -> str:
    if not s:
        return s
    return _SANITIZE_RE.sub("", s).strip()


def read_id3v2_metadata(file_path):
    """Extract metadata (artist, title, album, duration) across common audio types.
    Returns dict or None on failure.
    """
    try:
        audio = MutagenFile(file_path, easy=False)
        if audio is None:
            logger.debug("mutagen could not identify file: %s", file_path)
            return None

        # default values
        artist = None
        title = None
        album = None
        duration = None

        # MP3 (ID3)
        if file_path.lower().endswith('.mp3') or audio.__class__.__name__ == 'MP3':
            try:
                mp3 = MP3(file_path)
                duration = int(mp3.info.length) if mp3.info else None
                try:
                    tags = ID3(file_path)
                    artist = _sanitize_text(str(tags.get('TPE1')))
                    title = _sanitize_text(str(tags.get('TIT2')))
                    album = _sanitize_text(str(tags.get('TALB')))
                except ID3NoHeaderError:
                    # no ID3 header
                    artist = None
                    title = None
                    album = None
            except Exception as e:
                logger.exception("MP3 metadata read error: %s", e)

        # MP4 / M4A
        elif file_path.lower().endswith(('.m4a', '.mp4')) or audio.__class__.__name__ == 'MP4':
            try:
                mp4 = MP4(file_path)
                duration = int(mp4.info.length) if mp4.info else None
                tags = mp4.tags
                if tags:
                    artist = _sanitize_text(';'.join(tags.get('\u00A9ART', []))) if tags.get('\u00A9ART') else None
                    title = _sanitize_text(';'.join(tags.get('\u00A9nam', []))) if tags.get('\u00A9nam') else None
                    album = _sanitize_text(';'.join(tags.get('\u00A9alb', []))) if tags.get('\u00A9alb') else None
            except Exception as e:
                logger.exception("MP4 metadata read error: %s", e)

        # FLAC / OGG
        elif file_path.lower().endswith(('.flac', '.ogg')) or audio.__class__.__name__ == 'FLAC':
            try:
                fl = FLAC(file_path)
                duration = int(fl.info.length) if fl.info else None
                tags = fl.tags
                if tags:
                    artist = _sanitize_text(';'.join(tags.get('artist', []))) if tags.get('artist') else None
                    title = _sanitize_text(';'.join(tags.get('title', []))) if tags.get('title') else None
                    album = _sanitize_text(';'.join(tags.get('album', []))) if tags.get('album') else None
            except Exception as e:
                logger.exception("FLAC/OGG metadata read error: %s", e)

        # Fallback to generic tags
        if not artist or not title:
            try:
                tags = getattr(audio, 'tags', None)
                if tags:
                    # try common fields
                    artist = artist or _sanitize_text(str(tags.get('artist') or tags.get('TPE1') or ''))
                    title = title or _sanitize_text(str(tags.get('title') or tags.get('TIT2') or ''))
                    album = album or _sanitize_text(str(tags.get('album') or tags.get('TALB') or ''))
            except Exception:
                pass

        # Last resort: use filename
        if not title:
            title = os.path.splitext(os.path.basename(file_path))[0]
        if not artist:
            artist = 'Unknown Artist'
        if not album:
            album = 'Unknown Album'

        metadata = {
            'artist': artist[:200].strip() if artist else 'Unknown Artist',
            'title': title[:200].strip() if title else os.path.splitext(os.path.basename(file_path))[0],
            'album': album[:200].strip() if album else 'Unknown Album',
            'duration': duration
        }
        logger.debug("Metadata extracted for %s: %s", file_path, metadata)
        return metadata

    except Exception as e:
        logger.exception("Error extracting metadata: %s", e)
        return None


def embed_lyrics_in_file(file_path, lyrics_text, lyrics_type="plain"):
    """Embed lyrics into file_path. lyrics_type is 'plain' or 'synced'.
    Returns True on success, False on failure.
    This function performs format detection and uses appropriate mutagen tags.
    """
    try:
        if not lyrics_text or not lyrics_text.strip():
            logger.error("Empty lyrics provided for embedding")
            return False

        audio = MutagenFile(file_path, easy=False)
        if audio is None:
            logger.error("Unsupported or unrecognized audio format for embedding: %s", file_path)
            return False

        logger.debug("Embedding lyrics (type=%s) into %s", lyrics_type, file_path)

        # MP3 / ID3
        if file_path.lower().endswith('.mp3') or audio.__class__.__name__ == 'MP3':
            try:
                try:
                    id3 = ID3(file_path)
                except ID3NoHeaderError:
                    # create ID3
                    mp3 = MP3(file_path)
                    mp3.add_tags()
                    mp3.save()
                    id3 = ID3(file_path)

                if lyrics_type == 'plain':
                    uslt = USLT(encoding=3, lang='eng', desc='', text=lyrics_text)
                    id3.delall('USLT')
                    id3.add(uslt)
                else:
                    # store synced as COMM with description 'synced' to preserve LRC
                    id3.delall('COMM')
                    comm = COMM(encoding=3, lang='eng', desc='synced', text=[lyrics_text])
                    id3.add(comm)

                id3.save(file_path)

                # verify
                loaded = ID3(file_path)
                if lyrics_type == 'plain':
                    if not loaded.getall('USLT'):
                        logger.error('USLT tag not present after save')
                        return False
                else:
                    comms = loaded.getall('COMM')
                    if not any(c.desc == 'synced' for c in comms):
                        logger.error('COMM synced not present after save')
                        return False

                logger.debug('MP3 embedding successful')
                return True
            except Exception as e:
                logger.exception('MP3 embedding error: %s', e)
                return False

        # MP4 / M4A
        elif file_path.lower().endswith(('.m4a', '.mp4')) or audio.__class__.__name__ == 'MP4':
            try:
                mp4 = MP4(file_path)
                if lyrics_type == 'plain':
                    mp4.tags['\u00A9lyr'] = [lyrics_text]
                else:
                    # no standard for synced in mp4; store under '----:com.apple.iTunes:lyrics' freeform atom
                    mp4.tags['\u00A9lyr'] = [lyrics_text]
                mp4.save()

                mp4r = MP4(file_path)
                if '\u00A9lyr' not in (mp4r.tags or {}):
                    logger.error('MP4 lyrics key missing after save')
                    return False
                logger.debug('MP4 embedding successful')
                return True
            except Exception as e:
                logger.exception('MP4 embedding error: %s', e)
                return False

        # FLAC / OGG
        elif file_path.lower().endswith(('.flac', '.ogg')) or audio.__class__.__name__ == 'FLAC':
            try:
                fl = FLAC(file_path)
                if lyrics_type == 'plain':
                    fl['LYRICS'] = lyrics_text
                else:
                    fl['SYNCEDLYRICS'] = lyrics_text
                fl.save()

                flr = FLAC(file_path)
                if lyrics_type == 'plain' and 'LYRICS' not in flr:
                    logger.error('FLAC LYRICS tag missing after save')
                    return False
                if lyrics_type != 'plain' and 'SYNCEDLYRICS' not in flr:
                    logger.error('FLAC SYNCEDLYRICS tag missing after save')
                    return False
                logger.debug('FLAC/OGG embedding successful')
                return True
            except Exception as e:
                logger.exception('FLAC/OGG embedding error: %s', e)
                return False

        else:
            logger.error('Unsupported format for embedding: %s', file_path)
            return False

    except Exception as e:
        logger.exception('Unexpected embedding error: %s', e)
        return False
