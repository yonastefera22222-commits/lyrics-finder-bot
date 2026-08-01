*** Begin Patch
*** Update File: main.py
@@
-from pathlib import Path
-import tempfile
+from pathlib import Path
+import tempfile
@@
 FILE_STORAGE_DIR = "./music_files"
 if not os.path.exists(FILE_STORAGE_DIR):
     os.makedirs(FILE_STORAGE_DIR)
+
+# Concurrency limiter for embedding (protect Render Hobby resources)
+from threading import BoundedSemaphore
+EMBED_SEMAPHORE = BoundedSemaphore(2)
@@
-        downloaded_file = bot.download_file(file_info.file_path)
-        file_name = f"music_{user_id}_{int(datetime.now().timestamp())}.mp3"
-        file_path = os.path.join(FILE_STORAGE_DIR, file_name)
-        
-        print(f"[DEBUG] Saving to persistent storage: {file_path}")
-        
-        with open(file_path, 'wb') as f:
-            f.write(downloaded_file)
+        downloaded_file = bot.download_file(file_info.file_path)
+        # Create unique per-request directory under /tmp to avoid collisions and follow ephemeral practice
+        safe_dir = f"music_{user_id}_{int(datetime.now().timestamp())}"
+        tmpdir = os.path.join('/tmp', safe_dir)
+        os.makedirs(tmpdir, exist_ok=True)
+        # Preserve original extension if present
+        ext = os.path.splitext(file_info.file_path)[1] or '.mp3'
+        file_name = f"song{ext}"
+        file_path = os.path.join(tmpdir, file_name)
+
+        print(f"[DEBUG] Saving to temp storage: {file_path}")
+
+        with open(file_path, 'wb') as f:
+            # downloaded_file is bytes
+            f.write(downloaded_file)
@@
-        try:
-            bot.delete_message(message.chat.id, progress_msg.message_id)
-        except:
-            pass
-
-        # Save tracking info
-        admin_db.save_file_tracking(file_name, file_path, track_id)
+        try:
+            bot.delete_message(message.chat.id, progress_msg.message_id)
+        except:
+            pass
+
+        # Save tracking info - store full path so embed callback can find it
+        admin_db.save_file_tracking(file_name, file_path, track_id)
@@
-        try:
-            # Get file path from database
-            file_info = admin_db.get_file_tracking(file_name)
-            if not file_info:
-                bot.edit_message_text("❌ File tracking not found.", call.message.chat.id, msg.message_id)
-                return
-            
-            file_path = file_info['file_path']
-            
-            # Verify file still exists
-            if not os.path.exists(file_path):
-                print(f"[ERROR] File not found: {file_path}")
-                bot.edit_message_text("❌ File expired. Please send the music file again.", call.message.chat.id, msg.message_id)
-                return
-            
-            lyrics_data = get_lyrics_by_track_id(track_id)
-            
-            if lyrics_type == "synced":
-                lyrics_content = lyrics_data.get('syncedLyrics')
-            else:
-                lyrics_content = lyrics_data.get('plainLyrics')
-            
-            if not lyrics_content:
-                bot.edit_message_text("❌ Lyrics unavailable for this type.", call.message.chat.id, msg.message_id)
-                return
-            
-            bot.edit_message_text("⏳ Embedding lyrics into file...", call.message.chat.id, msg.message_id)
-            
-            success = embed_lyrics_in_file(file_path, lyrics_content, lyrics_type)
-            
-            if not success:
-                bot.edit_message_text("❌ Failed to embed lyrics.", call.message.chat.id, msg.message_id)
-                return
+        try:
+            # Get file path from database
+            file_info = admin_db.get_file_tracking(file_name)
+            if not file_info:
+                bot.edit_message_text("❌ File tracking not found.", call.message.chat.id, msg.message_id)
+                return
+
+            file_path = file_info['file_path']
+
+            # Verify file still exists
+            if not os.path.exists(file_path):
+                print(f"[ERROR] File not found: {file_path}")
+                bot.edit_message_text("❌ File expired. Please send the music file again.", call.message.chat.id, msg.message_id)
+                return
+
+            lyrics_data = get_lyrics_by_track_id(track_id)
+
+            if lyrics_type == "synced":
+                lyrics_content = lyrics_data.get('syncedLyrics')
+            else:
+                lyrics_content = lyrics_data.get('plainLyrics')
+
+            if not lyrics_content:
+                bot.edit_message_text("❌ Lyrics unavailable for this type.", call.message.chat.id, msg.message_id)
+                return
+
+            bot.edit_message_text("⏳ Embedding lyrics into file...", call.message.chat.id, msg.message_id)
+
+            # Acquire semaphore to limit concurrent embeddings
+            acquired = EMBED_SEMAPHORE.acquire(timeout=60)
+            if not acquired:
+                bot.edit_message_text("❌ Server busy. Try again shortly.", call.message.chat.id, msg.message_id)
+                return
+
+            try:
+                success = embed_lyrics_in_file(file_path, lyrics_content, lyrics_type)
+            finally:
+                try:
+                    EMBED_SEMAPHORE.release()
+                except Exception:
+                    pass
+
+            if not success:
+                bot.edit_message_text("❌ Failed to embed lyrics.", call.message.chat.id, msg.message_id)
+                return
*** End Patch
