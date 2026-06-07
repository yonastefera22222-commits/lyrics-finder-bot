import struct
from mutagen.id3 import ID3, COMM

def read_id3v2_metadata(file_path):
    """Extract ID3v2 metadata using mutagen"""
    try:
        audio = ID3(file_path)
        
        artist = str(audio.get('TPE1', 'Unknown Artist')).strip()
        title = str(audio.get('TIT2', 'Unknown Track')).strip()
        album = str(audio.get('TALB', 'Unknown Album')).strip()
        
        metadata = {
            "artist": artist[:100],
            "title": title[:100],
            "album": album[:100],
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
    """Embed lyrics into MP3 file using mutagen"""
    try:
        # Load or create ID3 tag
        try:
            audio = ID3(file_path)
        except:
            from mutagen.mp3 import MP3
            audio = MP3(file_path)
            audio.add_tags()
        
        # Limit lyrics to 40KB
        if len(lyrics_text.encode('utf-8')) > 40 * 1024:
            lyrics_text = lyrics_text[:10000]
        
        # Add lyrics using COMM frame (common to both synced and plain)
        audio['COMM'] = COMM(
            encoding=3,
            lang='eng',
            desc=lyrics_type,
            text=[lyrics_text[:5000]]
        )
        
        # Save the tags
        audio.save(v2_version=3)
        print(f"[DEBUG] Lyrics ({lyrics_type}) embedded successfully: {len(lyrics_text)} chars")
        return True
        
    except Exception as e:
        print(f"[ERROR] Embed error: {e}")
        import traceback
        traceback.print_exc()
        return False
