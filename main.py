#!/usr/bin/env python3
import os
import json
import re
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Tuple
import requests
from PIL import Image
import pytesseract
from dotenv import load_dotenv
import subprocess

# Gemini imports optional
try:
    from google import genai
    from google.genai import types
    from pydantic import BaseModel
    HAS_GEMINI = True
except Exception:
    HAS_GEMINI = False

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')  # optional
QURAN_API_BASE = os.getenv('QURAN_API_BASE', 'https://api.alquran.cloud/v1')

STATE_FILE = 'state.json'
LOGS_DIR = Path('logs')
LOGS_DIR.mkdir(exist_ok=True)

# Gemini client
gemini_client = None
if HAS_GEMINI and GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        logger.warning(f"Could not initialize Gemini client: {e}")
        gemini_client = None

if HAS_GEMINI:
    class VerseIdentification(BaseModel):
        surah: Optional[int] = None
        ayah: Optional[int] = None
else:
    VerseIdentification = None


def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read state file: {e}")
    return {'last_surah': 2, 'last_ayah': 74}  # ضع آخر آية منشورة هنا


def save_state(surah: int, ayah: int):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'last_surah': surah, 'last_ayah': ayah}, f, indent=2, ensure_ascii=False)
        logger.info(f"State saved: Surah {surah}, Ayah {ayah}")
    except Exception as e:
        logger.error(f"Failed to save state: {e}")


def update_state_on_github():
    """
    بعد حفظ state.json، يعمل commit و push تلقائي للريبو
    """
    try:
        subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
        subprocess.run(["git", "config", "user.email", "actions@github.com"], check=True)
        subprocess.run(["git", "add", "state.json"], check=True)
        subprocess.run(["git", "commit", "-m", "Update last posted verse"], check=True)
        subprocess.run(["git", "push"], check=True)
        logger.info("✅ state.json updated and pushed to GitHub")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to update state.json on GitHub: {e}")


# باقي الدوال: get_latest_telegram_message, extract_text_from_image, parse_verse_locally,
# identify_verse_with_gemini, get_surah_info, compute_next_verse, fetch_verse, post_to_telegram
# يبقوا كما هم بدون تعديل

# هنا الجزء النهائي من run_once بعد تعديل النشر ليشمل GitHub push
def run_once():
    logger.info("="*60)
    logger.info("Starting Quran Verse Daily Poster")
    logger.info("="*60)

    state = load_state()
    current_surah = int(state.get('last_surah', 2))
    current_ayah = int(state.get('last_ayah', 74))

    next_surah, next_ayah = compute_next_verse(current_surah, current_ayah)
    verse_data = fetch_verse(next_surah, next_ayah)
    if not verse_data:
        logger.error("Failed to fetch verse")
        return

    verse_text = verse_data.get('text', '')
    surah_name = verse_data.get('surah', {}).get('name', f"Surah {next_surah}")

    message_to_post = f"<b>{surah_name} - آية {next_ayah}</b>\n\n{verse_text}"

    if post_to_telegram(message_to_post):
        save_state(next_surah, next_ayah)
        update_state_on_github()  # ← مهم، بعد حفظ state.json
        logger.info(f"✅ Successfully posted: {surah_name}, Ayah {next_ayah}")
    else:
        logger.error("Failed to post to Telegram")

    logger.info("="*60)
    logger.info("Run completed")
    logger.info("="*60)


if __name__ == '__main__':
    run_once()
