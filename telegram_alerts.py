import urllib.request
import urllib.parse
import json
import os
import logging
import ssl
from config import TELEGRAM_ENABLED, TELEGRAM_TOKEN

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_config.json")

def load_chat_id():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)
                return data.get("chat_id")
        except:
            pass
    return None

def save_chat_id(chat_id):
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump({"chat_id": chat_id}, f, indent=4)
        logging.info(f"💾 Telegram Chat ID {chat_id} saved successfully.")
    except Exception as e:
        logging.error(f"Failed to save Telegram Chat ID: {e}")

def get_chat_id_from_updates():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10, context=ssl._create_unverified_context()) as response:
            res = json.loads(response.read().decode('utf-8'))
            if res.get("ok") and res.get("result"):
                for update in reversed(res["result"]):
                    message = update.get("message")
                    if message:
                        chat = message.get("chat")
                        if chat:
                            chat_id = chat.get("id")
                            if chat_id:
                                return chat_id
    except Exception as e:
        logging.warning(f"Failed to get Telegram updates: {e}")
    return None

def get_updates(offset=None, timeout=0):
    """Short-poll getUpdates() call to check for remote commands (/stop, /status).
    offset - which update_id to return from (inclusive) (already-processed old
    ones are skipped). Returns a list of 'result' items, or [] on error/empty queue."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        query = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{url}?{query}", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=timeout + 10, context=ssl._create_unverified_context()) as response:
            res = json.loads(response.read().decode('utf-8'))
            if res.get("ok"):
                return res.get("result", [])
    except Exception as e:
        logging.warning(f"Failed to get Telegram getUpdates(): {e}")
    return []


def send_telegram_message(message):
    if not TELEGRAM_ENABLED:
        return False

    chat_id = load_chat_id()
    if not chat_id:
        logging.info("⏳ Telegram Chat ID not found. Trying to get it from recent messages...")
        chat_id = get_chat_id_from_updates()
        if chat_id:
            save_chat_id(chat_id)
            send_telegram_message("🔌 *Connection activated!* This bot will now send messages to this chat.")
        else:
            logging.warning("⚠️ Telegram message not sent - no Chat ID. Send a message to your bot first.")
            return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        data = urllib.parse.urlencode(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10, context=ssl._create_unverified_context()) as response:
            res = json.loads(response.read().decode('utf-8'))
            return res.get("ok", False)
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")
        return False
