import sqlite3
import os

DB_PATH = "bot_data.db"

def setup_db():
    """Initialize admin database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            user_id TEXT PRIMARY KEY,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS file_tracking (
            file_name TEXT PRIMARY KEY,
            file_path TEXT,
            track_id TEXT,
            lyrics_type TEXT,
            file_id TEXT,
            user_id TEXT,
            original_name TEXT,
            artist TEXT,
            title TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    _migrate_tracking_table(cursor)
    conn.commit()
    conn.close()

def _migrate_tracking_table(cursor):
    """Add missing columns to older databases (safe to run every start-up)."""
    try:
        cursor.execute("PRAGMA table_info(file_tracking)")
        existing = {row[1] for row in cursor.fetchall()}
        for column, ddl in (
            ("file_id", "ALTER TABLE file_tracking ADD COLUMN file_id TEXT"),
            ("user_id", "ALTER TABLE file_tracking ADD COLUMN user_id TEXT"),
            ("original_name", "ALTER TABLE file_tracking ADD COLUMN original_name TEXT"),
            ("artist", "ALTER TABLE file_tracking ADD COLUMN artist TEXT"),
            ("title", "ALTER TABLE file_tracking ADD COLUMN title TEXT"),
        ):
            if column not in existing:
                cursor.execute(ddl)
                print(f"[DEBUG] Migrated file_tracking: added {column}")
    except Exception as e:
        print(f"[ERROR] Migration error: {e}")

def add_admin(user_id):
    """Add admin user"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (str(user_id),))
        conn.commit()
    except Exception as e:
        print(f"[ERROR] Add admin error: {e}")
    finally:
        conn.close()

def remove_admin(user_id):
    """Remove admin user"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute('DELETE FROM admins WHERE user_id = ?', (str(user_id),))
        conn.commit()
    except Exception as e:
        print(f"[ERROR] Remove admin error: {e}")
    finally:
        conn.close()

def is_admin(user_id):
    """Check if user is admin"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT 1 FROM admins WHERE user_id = ?', (str(user_id),))
        result = cursor.fetchone()
        return result is not None
    except Exception as e:
        print(f"[ERROR] Is admin error: {e}")
        return False
    finally:
        conn.close()

def get_all_admins():
    """Get all admin user IDs"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT user_id FROM admins ORDER BY added_at DESC')
        return [row[0] for row in cursor.fetchall()]
    except Exception as e:
        print(f"[ERROR] Get admins error: {e}")
        return []
    finally:
        conn.close()

def save_file_tracking(file_name, file_path, track_id, lyrics_type="plain",
                       file_id=None, user_id=None, original_name=None,
                       artist=None, title=None):
    """Save file tracking info.

    file_id lets the bot re-download the song from Telegram later, which keeps
    the "Lyricize" button working after a Render restart / free-tier spin-down
    wipes the local /tmp copy. artist/title let the bot search lrclib again if
    the saved track id ever fails to load.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT OR REPLACE INTO file_tracking
                (file_name, file_path, track_id, lyrics_type, file_id, user_id,
                 original_name, artist, title)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (file_name, file_path, track_id, lyrics_type, file_id,
              None if user_id is None else str(user_id), original_name,
              artist, title))
        conn.commit()
        print(f"[DEBUG] File tracking saved: {file_name}")
    except Exception as e:
        print(f"[ERROR] Save file tracking error: {e}")
    finally:
        conn.close()

def get_file_tracking(file_name):
    """Get file tracking info"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute('''
            SELECT file_path, track_id, lyrics_type, file_id, user_id,
                   original_name, artist, title
            FROM file_tracking WHERE file_name = ?
        ''', (file_name,))
        result = cursor.fetchone()
        if result:
            return {
                "file_path": result[0],
                "track_id": result[1],
                "lyrics_type": result[2],
                "file_id": result[3],
                "user_id": result[4],
                "original_name": result[5],
                "artist": result[6],
                "title": result[7],
            }
        return None
    except Exception as e:
        print(f"[ERROR] Get file tracking error: {e}")
        return None
    finally:
        conn.close()

def get_tracking_by_track_id(track_id):
    """Get the newest tracking row for a lyrics track id"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute('''
            SELECT file_name, file_path, track_id, lyrics_type, file_id,
                   user_id, original_name, artist, title
            FROM file_tracking WHERE track_id = ?
            ORDER BY created_at DESC LIMIT 1
        ''', (str(track_id),))
        result = cursor.fetchone()
        if result:
            return {
                "file_name": result[0],
                "file_path": result[1],
                "track_id": result[2],
                "lyrics_type": result[3],
                "file_id": result[4],
                "user_id": result[5],
                "original_name": result[6],
                "artist": result[7],
                "title": result[8],
            }
        return None
    except Exception as e:
        print(f"[ERROR] Get tracking by track id error: {e}")
        return None
    finally:
        conn.close()

def update_file_path(file_name, file_path):
    """Point a tracking row at a new local path (used after a re-download)."""
    if not file_name:
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute('''
            UPDATE file_tracking SET file_path = ? WHERE file_name = ?
        ''', (file_path, file_name))
        conn.commit()
    except Exception as e:
        print(f"[ERROR] Update file path error: {e}")
    finally:
        conn.close()

def cleanup_old_tracked_files(max_age_hours=4):
    """Clean up old file tracking records"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute('''
            SELECT file_path FROM file_tracking 
            WHERE datetime(created_at) < datetime('now', ? || ' hours')
        ''', (f'-{max_age_hours}',))
        old_files = cursor.fetchall()
        for (file_path,) in old_files:
            try:
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
                    print(f"[DEBUG] Deleted tracked file: {file_path}")
            except Exception as e:
                print(f"[ERROR] Could not delete {file_path}: {e}")
        
        cursor.execute('''
            DELETE FROM file_tracking WHERE datetime(created_at) < datetime('now', ? || ' hours')
        ''', (f'-{max_age_hours}',))
        conn.commit()
    except Exception as e:
        print(f"[ERROR] Cleanup tracked files error: {e}")
    finally:
        conn.close()
