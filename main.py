import os
import sqlite3
import telebot
import requests
from telebot import types

# =========================
# CONFIGURATION
# =========================

N8N_WEBHOOK_URL = (
    "https://shezar.app.n8n.cloud/webhook/c10a4de5-5562-40d8-83fb-2dad504493ba"
)

API_TOKEN = os.getenv("API_TOKEN")

if not API_TOKEN:
    raise ValueError("API_TOKEN environment variable is not set")

bot = telebot.TeleBot(API_TOKEN)

VOLUME_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "data"

os.makedirs(VOLUME_PATH, exist_ok=True)

DB_FILE = os.path.join(VOLUME_PATH, "user.db")


# =========================
# DATABASE
# =========================

def setup_database():
    with sqlite3.connect(DB_FILE) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS user (
                chat_id TEXT,
                memory_key TEXT,
                memory_value TEXT,
                PRIMARY KEY (chat_id, memory_key)
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                chat_id TEXT PRIMARY KEY,
                current_state TEXT
            )
        """)


def normalize_key(key):
    return key.strip().lower().replace(" ", "_")


def get_user_state(chat_id):
    with sqlite3.connect(DB_FILE) as connection:
        row = connection.execute(
            "SELECT current_state FROM bot_state WHERE chat_id = ?",
            (str(chat_id),)
        ).fetchone()

    return row[0] if row else "Menu"


def update_user_state(chat_id, state):
    with sqlite3.connect(DB_FILE) as connection:
        connection.execute("""
            INSERT OR REPLACE INTO bot_state
            (chat_id, current_state)
            VALUES (?, ?)
        """, (str(chat_id), state))


def recall_fact(chat_id, key):
    key = normalize_key(key)

    with sqlite3.connect(DB_FILE) as connection:
        row = connection.execute("""
            SELECT memory_value
            FROM user
            WHERE chat_id = ? AND memory_key = ?
        """, (str(chat_id), key)).fetchone()

    return row[0] if row else None


def remembered_fact(chat_id, key, value):
    key = normalize_key(key)
    value = str(value).strip()

    if not key or key == "temp_key":
        raise ValueError("Please choose a valid memory key.")

    with sqlite3.connect(DB_FILE) as connection:
        connection.execute("""
            INSERT OR REPLACE INTO user
            (chat_id, memory_key, memory_value)
            VALUES (?, ?, ?)
        """, (str(chat_id), key, value))


def recall_all_fact(chat_id):
    with sqlite3.connect(DB_FILE) as connection:
        return connection.execute("""
            SELECT memory_key, memory_value
            FROM user
            WHERE chat_id = ?
            ORDER BY memory_key
        """, (str(chat_id),)).fetchall()


def save_pending_keys(chat_id, keys):
    # Save keys as ordinary comma-separated text.
    with sqlite3.connect(DB_FILE) as connection:
        connection.execute("""
            INSERT OR REPLACE INTO user
            (chat_id, memory_key, memory_value)
            VALUES (?, 'temp_key', ?)
        """, (str(chat_id), ",".join(keys)))


def clear_pending_keys(chat_id):
    with sqlite3.connect(DB_FILE) as connection:
        connection.execute("""
            DELETE FROM user
            WHERE chat_id = ? AND memory_key = 'temp_key'
        """, (str(chat_id),))


# Create the database tables.
setup_database()


# =========================
# MENU
# =========================

def main_menu():
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=False
    )

    keyboard.add("View Profile", "Add More Info")
    keyboard.add("Fetch Data", "Fetch All Data")

    return keyboard


def show_menu(chat_id, message_text):
    bot.send_message(
        chat_id,
        message_text,
        reply_markup=main_menu()
    )


# =========================
# N8N AI INTEGRATION
# =========================

def send_to_n8n(chat_id, user_message):
    try:
        memories = [
            (key, value)
            for key, value in recall_all_fact(chat_id)
            if key != "temp_key"
        ]

        memory_text = "\n".join(
            f"{key}: {value}"
            for key, value in memories
        )

        response = requests.post(
            N8N_WEBHOOK_URL,
            json={
                "chat_id": chat_id,
                "message": user_message,
                "memory": memory_text
            },
            timeout=30
        )

        response.raise_for_status()

        result = (
            response.json()
            if response.text.strip()
            else {}
        )

        reply = None

        if isinstance(result, dict):
            reply = (
                result.get("reply")
                or result.get("output")
                or result.get("response")
                or result.get("text")
            )

            if reply is None and isinstance(
                result.get("data"), dict
            ):
                data = result["data"]

                reply = (
                    data.get("reply")
                    or data.get("output")
                    or data.get("response")
                    or data.get("text")
                )

        elif isinstance(result, list) and result:
            first = result[0]

            if isinstance(first, dict):
                reply = (
                    first.get("reply")
                    or first.get("output")
                    or first.get("response")
                    or first.get("text")
                )

            elif isinstance(first, str):
                reply = first

        elif isinstance(result, str):
            reply = result

        if not isinstance(reply, str) or not reply.strip():
            bot.send_message(
                chat_id,
                "n8n received your message but returned no reply. "
                "Please check your n8n workflow response."
            )
            return

        reply = reply.strip()

        # Send long replies in smaller messages.
        for start in range(0, len(reply), 4000):
            bot.send_message(
                chat_id,
                reply[start:start + 4000]
            )

    except requests.RequestException as error:
        print(f"n8n request error: {error}")

        bot.send_message(
            chat_id,
            "Sorry, I couldn't reach my AI service. "
            "Please try again later."
        )

    except (ValueError, TypeError) as error:
        print(f"n8n response error: {error}")

        bot.send_message(
            chat_id,
            "I received an unexpected reply from the AI service."
        )


# =========================
# START COMMAND
# =========================

@bot.message_handler(commands=["start"])
def start(message):
    chat_id = message.chat.id

    name = recall_fact(chat_id, "name")

    if name:
        update_user_state(chat_id, "Menu")

        show_menu(
            chat_id,
            f"Hello {name}! Welcome back."
        )

    else:
        update_user_state(chat_id, "ASK_NAME")

        bot.send_message(
            chat_id,
            "Hi! Welcome to the bot.\n"
            "What is your name?",
            reply_markup=types.ReplyKeyboardRemove()
        )


# =========================
# MAIN MESSAGE HANDLER
# =========================

@bot.message_handler(
    func=lambda message: (
        message.text is not None
        and not message.text.startswith("/")
    ),
    content_types=["text"]
)
def conversational(message):
    chat_id = message.chat.id
    user_text = message.text.strip()

    state = get_user_state(chat_id)

    if not user_text:
        bot.send_message(
            chat_id,
            "Please enter some text."
        )
        return

    # =========================
    # CREATE USER PROFILE
    # =========================

    if state == "ASK_NAME":
        remembered_fact(chat_id, "name", user_text)

        update_user_state(chat_id, "ASK_ADDRESS")

        bot.send_message(
            chat_id,
            "What is your address?"
        )
        return

    if state == "ASK_ADDRESS":
        remembered_fact(chat_id, "address", user_text)

        update_user_state(chat_id, "ASK_PHONE")

        bot.send_message(
            chat_id,
            "What is your phone number?"
        )
        return

    if state == "ASK_PHONE":
        remembered_fact(chat_id, "phone", user_text)

        update_user_state(chat_id, "Menu")

        show_menu(
            chat_id,
            "Your profile is ready!"
        )
        return

    # =========================
    # MAIN MENU OPTIONS
    # =========================

    if state == "Menu":

        # View Profile
        if user_text == "View Profile":
            name = recall_fact(chat_id, "name") or "not provided"
            address = recall_fact(chat_id, "address") or "not provided"
            phone = recall_fact(chat_id, "phone") or "not provided"

            show_menu(
                chat_id,
                f"Your Profile\n"
                f"Name: {name}\n"
                f"Address: {address}\n"
                f"Phone: {phone}"
            )
            return

        # Add More Info
        if user_text == "Add More Info":
            update_user_state(chat_id, "CHOOSE_KEY")

            bot.send_message(
                chat_id,
                "What information would you like to save?\n"
                "Enter key names separated by commas.\n"
                "Example: hobby, colour, country",
                reply_markup=types.ReplyKeyboardRemove()
            )
            return

        # Fetch Data
        if user_text == "Fetch Data":
            update_user_state(chat_id, "ASK_TYPE")

            bot.send_message(
                chat_id,
                "What information would you like to recall?\n"
                "Example: hobby",
                reply_markup=types.ReplyKeyboardRemove()
            )
            return

        # Fetch All Data
        if user_text == "Fetch All Data":
            data = [
                (key, value)
                for key, value in recall_all_fact(chat_id)
                if key != "temp_key"
            ]

            if not data:
                show_menu(
                    chat_id,
                    "You have no saved information yet."
                )
                return

            lines = [
                f"• {key.replace('_', ' ').title()}: {value}"
                for key, value in data
            ]

            show_menu(
                chat_id,
                "Your Saved Information\n" + "\n".join(lines)
            )
            return

        # Send ordinary messages to n8n AI.
        send_to_n8n(chat_id, user_text)
        return

    # =========================
    # FETCH ONE SAVED VALUE
    # =========================

    if state == "ASK_TYPE":
        value = recall_fact(chat_id, user_text)

        update_user_state(chat_id, "Menu")

        if value is None:
            show_menu(
                chat_id,
                "No saved value found for that key."
            )
        else:
            show_menu(
                chat_id,
                f"{user_text.replace('_', ' ').title()}: {value}"
            )
        return

    # =========================
    # CHOOSE MULTIPLE KEYS
    # =========================

    if state == "CHOOSE_KEY":
        keys = []

        for item in user_text.split(","):
            key = normalize_key(item)

            if key and key not in keys:
                keys.append(key)

        if not keys:
            bot.send_message(
                chat_id,
                "Please enter at least one valid key."
            )
            return

        if "temp_key" in keys:
            bot.send_message(
                chat_id,
                "'temp_key' is reserved. Please choose another key."
            )
            return

        save_pending_keys(chat_id, keys)

        update_user_state(chat_id, "CHOOSE_VALUE")

        bot.send_message(
            chat_id,
            "Enter one value for each key in the same order:\n"
            + ", ".join(keys)
            + "\n\nSeparate values with commas.\n"
            "Example: gaming, grey, India"
        )
        return

    # =========================
    # SAVE MULTIPLE VALUES
    # =========================

    if state == "CHOOSE_VALUE":
        pending = recall_fact(chat_id, "temp_key")

        if not pending:
            update_user_state(chat_id, "Menu")

            show_menu(
                chat_id,
                "The pending form was not found. Please try again."
            )
            return

        keys = [
            key
            for key in pending.split(",")
            if key
        ]

        values = [
            value.strip()
            for value in user_text.split(",")
        ]

        if (
            len(values) != len(keys)
            or any(not value for value in values)
        ):
            bot.send_message(
                chat_id,
                f"You entered {len(values)} value(s), but there are "
                f"{len(keys)} key(s).\n"
                "Please enter exactly one value per key, "
                "separated by commas, in the same order."
            )
            return

        try:
            for key, value in zip(keys, values):
                remembered_fact(chat_id, key, value)

            clear_pending_keys(chat_id)

            update_user_state(chat_id, "Menu")

            show_menu(
                chat_id,
                f"Successfully saved {len(keys)} item(s)!"
            )

        except (sqlite3.Error, ValueError) as error:
            print(f"Memory saving error: {error}")

            bot.send_message(
                chat_id,
                "Sorry, I couldn't save your information. "
                "Please try again."
            )
        return

    # =========================
    # RECOVER FROM UNKNOWN STATE
    # =========================

    update_user_state(chat_id, "Menu")

    show_menu(
        chat_id,
        "Let's return to the main menu."
    )


# =========================
# START BOT
# =========================

print("Starting Telegram bot...")

bot.infinity_polling()
             
    
        
        