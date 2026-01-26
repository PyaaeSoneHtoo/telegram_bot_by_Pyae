import os
import subprocess
import sys
import asyncio
from dotenv import load_dotenv, set_key
from telethon import TelegramClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

async def verify_credentials(api_id, api_hash, phone, bot_token):
    print("\n⏳ Verifying credentials...")
    session_path = os.path.join(BASE_DIR, "manager_temp_session")
    client = TelegramClient(session_path, api_id, api_hash)
    try:
        await client.start(bot_token=bot_token)
        me = await client.get_me()
        print(f"✅ Success! Logged in as: {me.username}")
        await client.disconnect()
        # Remove temp session files
        if os.path.exists(f"{session_path}.session"):
            os.remove(f"{session_path}.session")
        return True
    except Exception as e:
        print(f"❌ Verification failed: {e}")
        return False

def setup_env():
    load_dotenv(ENV_PATH)
    
    existing = {
        "API_ID": os.getenv("API_ID"),
        "API_HASH": os.getenv("API_HASH"),
        "PHONE": os.getenv("PHONE"),
        "BOT_TOKEN": os.getenv("BOT_TOKEN")
    }

    if all(existing.values()):
        print("✅ Existing credentials found in .env")
        choice = input("Use existing credentials? (Y/n): ").strip().lower()
        if choice in ('', 'y', 'yes'):
            return existing

    print("\n=== Telegram Bot Manager Setup ===")
    api_id = input("Enter API_ID: ").strip()
    api_hash = input("Enter API_HASH: ").strip()
    phone = input("Enter Phone Number (with +): ").strip()
    bot_token = input("Enter BOT_TOKEN: ").strip()

    if not all([api_id, api_hash, phone, bot_token]):
        print("❌ All fields are required.")
        return None

    return {
        "API_ID": api_id,
        "API_HASH": api_hash,
        "PHONE": phone,
        "BOT_TOKEN": bot_token
    }

def save_to_env(creds):
    if not os.path.exists(ENV_PATH):
        open(ENV_PATH, 'w').close()
    
    for key, value in creds.items():
        set_key(ENV_PATH, key, value)
    print(f"✅ Credentials saved to {ENV_PATH}")

def run_bot_background():
    print("\n🚀 Starting bot in background...")
    bot_script = os.path.join(BASE_DIR, "bot.py")
    
    # Windows specific detached process
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                [sys.executable, bot_script],
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True
            )
        else:
            subprocess.Popen(
                [sys.executable, bot_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True
            )
        print("✅ Bot is now running in the background.")
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")

async def main():
    creds = setup_env()
    if not creds:
        return

    if await verify_credentials(creds["API_ID"], creds["API_HASH"], creds["PHONE"], creds["BOT_TOKEN"]):
        save_to_env(creds)
        run_bot_background()
        print("\n✨ Setup complete. You can close this window.")
    else:
        print("❌ Setup aborted due to verification failure.")

if __name__ == "__main__":
    asyncio.run(main())
