import sqlite3
import os
from datetime import datetime

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
            file_path TEXT UNIQUE,
            track_id TEXT,
            lyrics_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

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

def save_file_tracking(file_name, file_path, track_id, lyrics_type="plain"):
    """Save file tracking info"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT OR REPLACE INTO file_tracking (file_name, file_path, track_id, lyrics_type)
            VALUES (?, ?, ?, ?)
        ''', (file_name, file_path, track_id, lyrics_type))
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
            SELECT file_path, track_id, lyrics_type FROM file_tracking WHERE file_name = ?
        ''', (file_name,))
        result = cursor.fetchone()
        if result:
            return {"file_path": result[0], "track_id": result[1], "lyrics_type": result[2]}
        return None
    except Exception as e:
        print(f"[ERROR] Get file tracking error: {e}")
        return None
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
                if os.path.exists(file_path):
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
