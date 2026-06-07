import sqlite3
import os

DB_NAME = "admins.db"

def setup_db():
    """Initialize the admin database"""
    if not os.path.exists(DB_NAME):
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE admins (
                user_id TEXT PRIMARY KEY
            )
        ''')
        conn.commit()
        conn.close()

def add_admin(user_id):
    """Add a user as admin"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (str(user_id),))
        conn.commit()
    except Exception as e:
        print(f"Error adding admin: {e}")
    finally:
        conn.close()

def remove_admin(user_id):
    """Remove a user from admins"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM admins WHERE user_id = ?", (str(user_id),))
        conn.commit()
    except Exception as e:
        print(f"Error removing admin: {e}")
    finally:
        conn.close()

def is_admin(user_id):
    """Check if a user is an admin"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (str(user_id),))
        result = cursor.fetchone()
        return result is not None
    except Exception as e:
        print(f"Error checking admin: {e}")
        return False
    finally:
        conn.close()

def get_all_admins():
    """Get all admin user IDs"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT user_id FROM admins")
        admins = [row[0] for row in cursor.fetchall()]
        return admins
    except Exception as e:
        print(f"Error getting admins: {e}")
        return []
    finally:
        conn.close()
