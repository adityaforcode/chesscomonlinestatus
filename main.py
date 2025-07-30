import time
import requests
from datetime import datetime
import pytz
import threading
from flask import Flask
import os
from collections import defaultdict
import json

# === CONFIGURATION ===
BOT_ADMIN_ID = os.getenv("BOT_ADMIN_ID", "")
DB_CHANNEL_ID = os.getenv("DB_CHANNEL_ID", "") # e.g., "-1002688118367"
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_USERNAMES_PER_USER = 10
CHECK_INTERVAL = 30 # Check every 30 seconds for faster updates

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/115.0.0.0 Safari/537.36"
}
IST = pytz.timezone("Asia/Kolkata")

# === STATE MANAGEMENT (GLOBAL VARIABLES) ===
# A lock to ensure thread safety when accessing shared state
STATE_LOCK = threading.Lock()

# --- Shared State (Protected by STATE_LOCK) ---
AUTHORIZED_USERS = set()
PER_USER_LIMITS = {}
user_monitored = defaultdict(set)
LATEST_DB_MESSAGE_ID = None

# --- In-memory cache (also protected for consistency) ---
user_uuids = {} # username -> uuid
user_last_status = {} # username -> "online" / "offline" etc.
user_last_seen_unix = {} # username -> unix_timestamp
# -----------------------------------------------

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running ✅"

# === UTILITY FUNCTIONS ===

def send_telegram_message(text, chat_id, parse_mode=""):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    try:
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        print(f"[!] Telegram send message error: {e}")

def send_telegram_document(file_path, chat_id):
    global LATEST_DB_MESSAGE_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"

    # Delete the previous database message if we know its ID
    if LATEST_DB_MESSAGE_ID:
        delete_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
        requests.post(delete_url, data={"chat_id": chat_id, "message_id": LATEST_DB_MESSAGE_ID})

    with open(file_path, 'rb') as f:
        files = {'document': f}
        data = {"chat_id": chat_id}
        try:
            response = requests.post(url, files=files, data=data, timeout=10)
            if response.ok:
                result = response.json().get("result", {})
                # This write access to a global var should be inside the lock
                # that calls save_user_data()
                LATEST_DB_MESSAGE_ID = result.get("message_id")
                print(f"[INFO] New DB saved with message ID: {LATEST_DB_MESSAGE_ID}")
            else:
                print(f"[!] Failed to send file: {response.text}")
        except Exception as e:
            print(f"[!] Error sending document: {e}")

def save_user_data():
    # This function should only be called from within a `with STATE_LOCK:` block
    try:
        payload = {
            "user_monitored": {uid: list(usernames) for uid, usernames in user_monitored.items()},
            "limits": PER_USER_LIMITS,
            "authorized_users": list(AUTHORIZED_USERS)
        }
        with open("db.json", "w") as f:
            json.dump(payload, f)

        if DB_CHANNEL_ID:
            send_telegram_document("db.json", DB_CHANNEL_ID)
        else:
            print("[WARN] DB_CHANNEL_ID not set. Not saving to Telegram.")

    except Exception as e:
        print(f"[!] Failed to save data: {e}")

def load_user_data():
    global user_monitored, PER_USER_LIMITS, AUTHORIZED_USERS, LATEST_DB_MESSAGE_ID
    print("[INFO] Attempting to load data from Telegram channel...")

    if not DB_CHANNEL_ID:
        print("[WARN] DB_CHANNEL_ID not set. Skipping data load.")
        return

    try:
        # Get channel history to find the last db.json
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getChatHistory?chat_id={DB_CHANNEL_ID}&limit=100"
        resp = requests.get(url, timeout=10).json()

        if "result" in resp:
            for message in resp["result"]:
                doc = message.get("document")
                if doc and doc.get("file_name") == "db.json":
                    print("[INFO] Found db.json in Telegram history.")
                    file_id = doc["file_id"]
                    
                    file_info_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={file_id}"
                    file_info_resp = requests.get(file_info_url).json()
                    file_path = file_info_resp["result"]["file_path"]

                    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
                    file_content = requests.get(file_url).content
                    data = json.loads(file_content)

                    with STATE_LOCK:
                        user_monitored = defaultdict(set, {uid: set(usernames) for uid, usernames in data.get("user_monitored", {}).items()})
                        PER_USER_LIMITS.update(data.get("limits", {}))
                        AUTHORIZED_USERS = set(data.get("authorized_users", []))
                        LATEST_DB_MESSAGE_ID = message.get("message_id")

                    print(f"[✅] Successfully restored data from message ID {LATEST_DB_MESSAGE_ID}")
                    return

        print("[!] No db.json found in the last 100 messages of the Telegram channel history.")

    except Exception as e:
        print(f"[!] Failed to load data from Telegram channel: {e}")

def convert_unix_to_ist(unix_timestamp):
    if not unix_timestamp: return "Unknown"
    try:
        dt_utc = datetime.utcfromtimestamp(unix_timestamp).replace(tzinfo=pytz.utc)
        return dt_utc.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    except (ValueError, TypeError):
        return "Invalid Time"

def get_user_data_from_api(username):
    uuid_url = f"https://www.chess.com/callback/user/popup/{username}"
    online_url = f"https://api.chess.com/pub/player/{username}"
    try:
        uuid_resp = requests.get(uuid_url, headers=HEADERS, timeout=5)
        online_resp = requests.get(online_url, headers=HEADERS, timeout=5)

        uuid = uuid_resp.json().get("uuid") if uuid_resp.status_code == 200 else None
        last_online = online_resp.json().get("last_online") if online_resp.status_code == 200 else None
        
        return {"uuid": uuid, "last_online_unix": last_online}
    except requests.RequestException as e:
        print(f"[!] API request error for {username}: {e}")
        return {"uuid": None, "last_online_unix": None}

def get_all_monitored_usernames():
    # Helper to get a consistent snapshot of all usernames being monitored
    all_usernames = set()
    for usernames_set in user_monitored.values():
        all_usernames.update(usernames_set)
    return all_usernames

# === MONITORING LOOP ===
def monitor_loop():
    while True:
        try:
            with STATE_LOCK:
                # Get a snapshot of usernames to monitor for this cycle
                usernames_to_check = get_all_monitored_usernames()
                if not usernames_to_check:
                    time.sleep(CHECK_INTERVAL)
                    continue

                # 1. Fetch missing UUIDs and initial data
                for username in list(usernames_to_check): # Use list to allow removal
                    if username not in user_uuids:
                        data = get_user_data_from_api(username)
                        if data and data["uuid"]:
                            user_uuids[username] = data["uuid"]
                            user_last_seen_unix[username] = data["last_online_unix"]
                            print(f"[INFO] Fetched UUID for {username}")
                        else:
                            print(f"[WARN] Could not fetch UUID for {username}. Skipping for now.")
                            # Cannot check this user without a UUID
                            usernames_to_check.remove(username)
                
                # 2. Batch-fetch presence data for all users with UUIDs
                uuids_to_check = [user_uuids[u] for u in usernames_to_check if u in user_uuids]
                if not uuids_to_check:
                    time.sleep(CHECK_INTERVAL)
                    continue

                presence_url = f"https://www.chess.com/service/presence/users?ids={','.join(uuids_to_check)}"
                presence_resp = requests.get(presence_url, headers=HEADERS, timeout=10)
                
                if presence_resp.status_code != 200:
                    print(f"[!] Presence API failed with status {presence_resp.status_code}")
                    time.sleep(CHECK_INTERVAL)
                    continue
                
                presence_data = {user['userId']: user for user in presence_resp.json().get("users", [])}
                
                # Reverse map from uuid back to username
                uuid_to_username = {v: k for k, v in user_uuids.items()}

                # 3. Process results and send notifications
                for uuid, presence in presence_data.items():
                    username = uuid_to_username.get(uuid)
                    if not username: continue

                    new_status = presence.get("status")
                    previous_status = user_last_status.get(username)

                    if new_status == "online" and previous_status != "online":
                        # Find all users monitoring this username and notify them
                        for user_id, monitored_set in user_monitored.items():
                            if username in monitored_set:
                                msg = f"♟️ {username} is now ONLINE\nLast seen: {convert_unix_to_ist(user_last_seen_unix.get(username))}"
                                send_telegram_message(msg, user_id)
                    
                    # Update the last known status and last online time
                    user_last_status[username] = new_status
                    if new_status != 'online': # Update last seen time when they go offline
                        user_data = get_user_data_from_api(username)
                        if user_data and user_data['last_online_unix']:
                            user_last_seen_unix[username] = user_data['last_online_unix']


        except Exception as e:
            print(f"[!!!] CRITICAL ERROR in monitor_loop: {e}")

        time.sleep(CHECK_INTERVAL)


# === COMMAND HANDLER ===
def handle_commands():
    offset = None
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?timeout=60"
            if offset:
                url += f"&offset={offset}"
            
            updates = requests.get(url, timeout=65).json()

            if "result" in updates:
                for update in updates["result"]:
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id"))
                    user_id = str(msg.get("from", {}).get("id"))
                    text = msg.get("text", "").strip()

                    if not text: continue

                    # Admin commands
                    if user_id == BOT_ADMIN_ID:
                        if text.startswith("/authorize"):
                            parts = text.split()
                            if len(parts) == 2:
                                with STATE_LOCK:
                                    AUTHORIZED_USERS.add(parts[1])
                                    save_user_data()
                                send_telegram_message("✅ User authorized.", chat_id)
                        
                        elif text.startswith("/unauthorize"):
                            parts = text.split()
                            if len(parts) == 2 and parts[1] in AUTHORIZED_USERS:
                                user_to_remove = parts[1]
                                with STATE_LOCK:
                                    AUTHORIZED_USERS.remove(user_to_remove)
                                    # Full cleanup
                                    user_monitored.pop(user_to_remove, None)
                                    save_user_data()
                                send_telegram_message("❌ User unauthorized and data cleaned.", chat_id)

                    # Check authorization for user commands
                    if user_id not in AUTHORIZED_USERS:
                        if user_id != BOT_ADMIN_ID:
                            send_telegram_message("🚫 You are not authorized to use this bot.", chat_id)
                        continue

                    # User commands
                    if text.startswith("/add"):
                        parts = text.split()
                        if len(parts) == 2:
                            username = parts[1].lower()
                            with STATE_LOCK:
                                current_set = user_monitored[user_id]
                                limit = PER_USER_LIMITS.get(user_id, MAX_USERNAMES_PER_USER)
                                if username in current_set:
                                    send_telegram_message("⚠️ You are already monitoring this username.", chat_id)
                                elif len(current_set) >= limit:
                                    send_telegram_message(f"⚠️ Limit of {limit} usernames reached.", chat_id)
                                else:
                                    current_set.add(username)
                                    save_user_data()
                                    send_telegram_message(f"✅ Username `{username}` added to your monitoring list.", chat_id, "MarkdownV2")
                        else:
                            send_telegram_message("Usage: /add <username>", chat_id)

                    elif text.startswith("/remove"):
                        parts = text.split()
                        if len(parts) == 2:
                            username = parts[1].lower()
                            with STATE_LOCK:
                                if username in user_monitored.get(user_id, set()):
                                    user_monitored[user_id].remove(username)
                                    # If no other user is monitoring this username, remove it from caches
                                    if not any(username in s for s in user_monitored.values()):
                                        user_uuids.pop(username, None)
                                        user_last_status.pop(username, None)
                                        user_last_seen_unix.pop(username, None)
                                    save_user_data()
                                    send_telegram_message(f"✅ Username `{username}` removed.", chat_id, "MarkdownV2")
                                else:
                                    send_telegram_message("❌ Username not found in your list.", chat_id)
                        else:
                            send_telegram_message("Usage: /remove <username>", chat_id)

                    elif text.startswith("/list"):
                        with STATE_LOCK:
                            monitored_list = sorted(list(user_monitored.get(user_id, set())))
                        if not monitored_list:
                            send_telegram_message("You are not monitoring any users.", chat_id)
                        else:
                            msg = "Monitoring the following users:\n" + "\n".join([f"- `{u}`" for u in monitored_list])
                            send_telegram_message(msg, chat_id, "MarkdownV2")
                    
                    elif text.startswith("/status"):
                        lines = ["♟️ **Player Status**\n"]
                        with STATE_LOCK:
                            monitored_list = sorted(list(user_monitored.get(user_id, set())))
                            if not monitored_list:
                                send_telegram_message("You are not monitoring any users. Use /add <username> to start.", chat_id)
                                continue

                            for username in monitored_list:
                                status = user_last_status.get(username, "UNKNOWN").upper()
                                last_seen = convert_unix_to_ist(user_last_seen_unix.get(username))
                                lines.append(f"• **{username}**: {status} \n  (Last Seen: {last_seen})")
                        
                        send_telegram_message("\n".join(lines), chat_id, "Markdown")


        except Exception as e:
            print(f"[!] Command loop error: {e}")
            time.sleep(5) # Wait a bit before retrying on error

# === START EVERYTHING ===
if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not BOT_ADMIN_ID:
        raise ValueError("BOT_TOKEN and BOT_ADMIN_ID environment variables must be set.")
    
    load_user_data()
    
    # Start background threads
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=handle_commands, daemon=True).start()
    
    # Start Flask web server to keep the service alive
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting Flask server on port {port}...")
    app.run(host="0.0.0.0", port=port)
