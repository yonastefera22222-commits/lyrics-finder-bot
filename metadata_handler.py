import struct
from mutagen.id3 import ID3, COMM
from mutagen.mp3 import MP3
import os

def read_id3v2_metadata(file_path):
    """Extract ID3v2 metadata using mutagen with better error handling"""
    try:
        # Try to read ID3 tags
        try:
            audio = ID3(file_path)
            artist = str(audio.get('TPE1', 'Unknown Artist')).strip()
            title = str(audio.get('TIT2', 'Unknown Track')).strip()
            album = str(audio.get('TALB', 'Unknown Album')).strip()
        except:
            # Fallback: try with MP3 object
            print("[DEBUG] ID3 tag not found, attempting MP3 fallback...")
            audio = MP3(file_path)
            try:
                tags = audio.tags
                if tags is None:
                    print("[DEBUG] No tags found, using filename")
                    artist = "Unknown Artist"
                    title = os.path.splitext(os.path.basename(file_path))[0]
                else:
                    artist = str(tags.get('TPE1', 'Unknown Artist')).strip()
                    title = str(tags.get('TIT2', 'Unknown Track')).strip()
                    album = str(tags.get('TALB', 'Unknown Album')).strip()
            except:
                # Last resort: use filename
                print("[DEBUG] Using filename as title")
                artist = "Unknown Artist"
                title = os.path.splitext(os.path.basename(file_path))[0]
                album = "Unknown Album"
        
        # Ensure we have valid data
        if not title or title == "Unknown Track":
            title = os.path.splitext(os.path.basename(file_path))[0]
        
        metadata = {
            "artist": artist[:100].strip() or "Unknown Artist",
            "title": title[:100].strip() or "Unknown Track",
            "album": album[:100].strip() or "Unknown Album",
            "duration": None
        }
        
        print(f"[DEBUG] Metadata extracted: Artist={metadata['artist']}, Title={metadata['title']}")
        return metadata
        
    except Exception as e:
        print(f"[ERROR] Metadata extraction error: {e}")
        import traceback
        traceback.print_exc()
        return None

def embed_lyrics_in_file(file_path, lyrics_text, lyrics_type="plain"):
    """Embed lyrics into MP3 file using mutagen with validation"""
    try:
        if not lyrics_text or len(lyrics_text.strip()) == 0:
            print("[ERROR] Lyrics text is empty")
            return False
        
        # Load or create ID3 tag
        try:
            audio = ID3(file_path)
        except:
            print("[DEBUG] Creating new ID3 tag...")
            audio = MP3(file_path)
            audio.add_tags()
        
        # Limit lyrics to 40KB
        if len(lyrics_text.encode('utf-8')) > 40 * 1024:
            print(f"[DEBUG] Lyrics too large ({len(lyrics_text)} chars), truncating...")
            lyrics_text = lyrics_text[:10000]
        
        # Add lyrics using COMM frame (common to both synced and plain)
        try:
            audio['COMM'] = COMM(
                encoding=3,
                lang='eng',
                desc=lyrics_type,
                text=[lyrics_text[:5000]]
            )
        except Exception as comm_error:
            print(f"[ERROR] COMM frame error: {comm_error}")
            # Try without description
            audio['COMM'] = COMM(
                encoding=3,
                lang='eng',
                desc='',
                text=[lyrics_text[:5000]]
            )
        
        # Save the tags
        audio.save(v2_version=3, padding=lambda x: 2048)
        print(f"[DEBUG] Lyrics ({lyrics_type}) embedded successfully: {len(lyrics_text)} chars")
        return True
        
    except Exception as e:
        print(f"[ERROR] Embed error: {e}")
        import traceback
        traceback.print_exc()
        return False
