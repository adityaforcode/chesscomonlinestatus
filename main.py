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
AUTHORIZED_USERS = set()
BOT_ADMIN_ID = os.getenv("BOT_ADMIN_ID", "")
MAX_USERNAMES_PER_USER = 10
PER_USER_LIMITS = {}
DB_CHANNEL_ID = os.getenv("DB_CHANNEL_ID", "-1002688118367")
CHECK_INTERVAL = 60
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/115.0.0.0 Safari/537.36"
}
IST = pytz.timezone("Asia/Kolkata")

user_uuids = {}
user_last_status = {}
user_last_seen_unix = {}
user_monitored = defaultdict(list)
all_monitored_usernames = set()
LATEST_DB_MESSAGE_ID = None

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running ✅"

# === UTILITY FUNCTIONS ===

def send_telegram_message(text, chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text}
    try:
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        print(f"[!] Telegram error: {e}")

def send_telegram_document(file_path, chat_id):
    global LATEST_DB_MESSAGE_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"

    if LATEST_DB_MESSAGE_ID:
        delete_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
        delete_response = requests.post(delete_url, data={"chat_id": chat_id, "message_id": LATEST_DB_MESSAGE_ID})
        print(f"[DEBUG] Delete response: {delete_response.status_code}, {delete_response.text}")

    with open(file_path, 'rb') as f:
        files = {'document': f}
        data = {"chat_id": chat_id}
        try:
            response = requests.post(url, files=files, data=data, timeout=10)
            print(f"[DEBUG] Upload response: {response.status_code}, {response.text}")
            if response.ok:
                result = response.json().get("result", {})
                LATEST_DB_MESSAGE_ID = result.get("message_id")
            else:
                print(f"[!] Failed to send file: {response.text}")
        except Exception as e:
            print(f"[!] Error sending document: {e}")

def save_user_data():
    try:
        payload = {
            "user_monitored": dict(user_monitored),
            "limits": PER_USER_LIMITS,
            "authorized_users": list(AUTHORIZED_USERS)
        }
        with open("db.json", "w") as f:
            json.dump(payload, f)
        send_telegram_document("db.json", DB_CHANNEL_ID)
    except Exception as e:
        print(f"[!] Failed to save data to channel: {e}")

def load_user_data():
    global user_monitored, PER_USER_LIMITS, AUTHORIZED_USERS, LATEST_DB_MESSAGE_ID

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        resp = requests.get(url).json()

        if "result" in resp:
            for msg in reversed(resp["result"]):
                message = msg.get("message", {})
                doc = message.get("document")

                if doc and doc.get("file_name") == "db.json":
                    print("[INFO] Found db.json in Telegram updates.")
                    file_id = doc.get("file_id")

                    file_info = requests.get(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={file_id}"
                    ).json()
                    file_path = file_info["result"]["file_path"]

                    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
                    file_content = requests.get(file_url).content

                    data = json.loads(file_content)

                    user_monitored = defaultdict(list, data.get("user_monitored", {}))
                    PER_USER_LIMITS.update(data.get("limits", {}))
                    AUTHORIZED_USERS = set(data.get("authorized_users", []))

                    LATEST_DB_MESSAGE_ID = message.get("message_id")

                    for usernames in user_monitored.values():
                        all_monitored_usernames.update(usernames)

                    print("[✅] Successfully restored data from db.json")
                    return

        print("[!] No db.json found in Telegram channel history.")

    except Exception as e:
        print(f"[!] Failed to load data from Telegram channel: {e}")

def convert_unix_to_ist(unix_timestamp):
    if not unix_timestamp:
        return "Unknown"
    try:
        dt_utc = datetime.utcfromtimestamp(unix_timestamp).replace(tzinfo=pytz.utc)
        dt_ist = dt_utc.astimezone(IST)
        return dt_ist.strftime("%Y-%m-%d %H:%M:%S IST")
    except:
        return "Invalid Time"

def get_user_data(username):
    uuid_url = f"https://www.chess.com/callback/user/popup/{username}"
    online_url = f"https://api.chess.com/pub/player/{username}"
    uuid = None
    last_online_unix = None
    try:
        r1 = requests.get(uuid_url, headers=HEADERS, timeout=5)
        if r1.status_code == 200:
            uuid = r1.json().get("uuid")
    except:
        pass
    try:
        r2 = requests.get(online_url, headers=HEADERS, timeout=5)
        if r2.status_code == 200:
            last_online_unix = r2.json().get("last_online")
    except:
        pass
    return {"uuid": uuid, "last_online_unix": last_online_unix}

def get_presence_data(uuid):
    try:
        url = f"https://www.chess.com/service/presence/users?ids={uuid}"
        resp = requests.get(url, headers=HEADERS, timeout=5)
        if resp.status_code == 200:
            users = resp.json().get("users", [])
            if users:
                return users[0]
    except:
        pass
    return None

# === MONITORING LOOP ===
def monitor_loop():
    global user_uuids, user_last_status, user_last_seen_unix
    while True:
        for user_id, usernames in user_monitored.items():
            for username in usernames:
                if username not in user_uuids:
                    data = get_user_data(username)
                    if data["uuid"]:
                        user_uuids[username] = data["uuid"]
                        user_last_seen_unix[username] = data["last_online_unix"]
                uuid = user_uuids.get(username)
                if not uuid:
                    continue
                presence = get_presence_data(uuid)
                if presence:
                    status = presence.get("status")
                    last_status = user_last_status.get(username)
                    if status == "online" and last_status != "online":
                        msg = f"♟ {username} is now ONLINE\nLast Online: {convert_unix_to_ist(user_last_seen_unix.get(username))}"
                        send_telegram_message(msg, user_id)
                    user_last_status[username] = status
        time.sleep(CHECK_INTERVAL)

# === COMMAND HANDLER ===
def handle_commands():
    offset = None
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            if offset:
                url += f"?offset={offset + 1}"
            updates = requests.get(url).json()
            if "result" in updates:
                for update in updates["result"]:
                    offset = update["update_id"]
                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id"))
                    user_id = str(msg.get("from", {}).get("id"))
                    text = msg.get("text", "").strip()
                    if not text:
                        continue

                    if text.startswith("/authorize") and user_id == BOT_ADMIN_ID:
                        parts = text.split()
                        if len(parts) == 2:
                            AUTHORIZED_USERS.add(parts[1])
                            save_user_data()
                            send_telegram_message("✅ User authorized.", chat_id)
                    elif text.startswith("/unauthorize") and user_id == BOT_ADMIN_ID:
                        parts = text.split()
                        if len(parts) == 2 and parts[1] in AUTHORIZED_USERS:
                            AUTHORIZED_USERS.remove(parts[1])
                            user_monitored.pop(parts[1], None)
                            save_user_data()
                            send_telegram_message("❌ User unauthorized.", chat_id)

                    elif user_id not in AUTHORIZED_USERS:
                        send_telegram_message("🚫 You are not authorized to use this bot.", chat_id)
                        continue

                    elif text.startswith("/add"):
                        parts = text.split()
                        if len(parts) == 2:
                            username = parts[1].lower()
                            current = user_monitored[user_id]
                            if username in current:
                                send_telegram_message("⚠ You are already monitoring this username.", chat_id)
                                continue
                            limit = PER_USER_LIMITS.get(user_id, MAX_USERNAMES_PER_USER)
                            if len(current) >= limit:
                                send_telegram_message("⚠ Limit reached.", chat_id)
                            else:
                                current.append(username)
                                all_monitored_usernames.add(username)
                                save_user_data()
                                send_telegram_message("✅ Username added.", chat_id)

                    elif text.startswith("/remove"):
                        parts = text.split()
                        if len(parts) == 2:
                            username = parts[1].lower()
                            if username in user_monitored[user_id]:
                                user_monitored[user_id].remove(username)
                                all_monitored_usernames.remove(username)
                                save_user_data()
                                send_telegram_message("✅ Username removed.", chat_id)
                            else:
                                send_telegram_message("❌ Username not found in your list.", chat_id)

                    elif text.startswith("/status"):
                        lines = ["♟ **Player Status:**"]
                        for username in user_monitored.get(user_id, []):
                            uuid = user_uuids.get(username)
                            presence = get_presence_data(uuid) if uuid else None
                            status = presence.get("status") if presence else "UNKNOWN"
                            last_seen = convert_unix_to_ist(user_last_seen_unix.get(username))
                            lines.append(f"• {username}: {status.upper()} (Last Online: {last_seen})")
                        send_telegram_message("\n".join(lines), chat_id)
        except Exception as e:
            print(f"[!] Command loop error: {e}")
        time.sleep(2)

# === START EVERYTHING ===
if __name__ == "__main__":
    load_user_data()
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=handle_commands, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
