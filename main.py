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

# إذا مش مثبت google-genai على Replit يمكن إزالة/تعطيل قسم Gemini أو تفعيله لو عندك المفتاح.
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

# إعداد عميل Gemini إن وُجد
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
    VerseIdentification = None  # placeholder


def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read state file: {e}")
            return {'last_surah': 1, 'last_ayah': 0}
    return {'last_surah': 1, 'last_ayah': 0}  # default start


def save_state(surah: int, ayah: int):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'last_surah': surah, 'last_ayah': ayah}, f, indent=2, ensure_ascii=False)
        logger.info(f"State saved: Surah {surah}, Ayah {ayah}")
    except Exception as e:
        logger.error(f"Failed to save state: {e}")


def save_run_log(log_data: Dict):
    try:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file = LOGS_DIR / f'run_{timestamp}.json'
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(log_data, f, indent=2, ensure_ascii=False)
        logger.info(f"Run log saved: {log_file}")
    except Exception as e:
        logger.error(f"Failed to save run log: {e}")


def get_latest_telegram_message() -> Optional[Dict]:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured")
        return None

    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates'
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data.get('ok') or not data.get('result'):
            return None
        updates = data['result']
        for update in reversed(updates):
            message = update.get('message') or update.get('channel_post') or {}
            if str(message.get('chat', {}).get('id')) == str(TELEGRAM_CHAT_ID):
                return message
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch Telegram updates: {e}")
        return None


def extract_text_from_image(photo_file_id: str) -> Optional[str]:
    if not TELEGRAM_BOT_TOKEN:
        return None
    try:
        file_url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={photo_file_id}'
        file_response = requests.get(file_url, timeout=10)
        file_response.raise_for_status()
        file_path = file_response.json()['result']['file_path']

        download_url = f'https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}'
        image_response = requests.get(download_url, timeout=10)
        image_response.raise_for_status()

        temp_image_path = 'temp_image.jpg'
        with open(temp_image_path, 'wb') as f:
            f.write(image_response.content)

        image = Image.open(temp_image_path)
        # تأكد إن tesseract مع Arabic مُركب على بيئة التشغيل (replit/محلي)
        text = pytesseract.image_to_string(image, lang='ara')
        os.remove(temp_image_path)
        logger.info(f"OCR extracted (cut): {text[:120]}")
        return text.strip()
    except Exception as e:
        logger.error(f"Failed to extract text from image: {e}")
        return None


def parse_verse_locally(text: str) -> Optional[Tuple[int, int]]:
    arabic_to_numeric = {'٠':'0','١':'1','٢':'2','٣':'3','٤':'4','٥':'5','٦':'6','٧':'7','٨':'8','٩':'9'}
    normalized_text = text
    for arabic, numeric in arabic_to_numeric.items():
        normalized_text = normalized_text.replace(arabic, numeric)
    patterns = [
        r'(?:سورة|سوره)\s*(\d{1,3})\s*(?:آية|ايه|اية)\s*(\d{1,4})',
        r'(\d{1,3})\s*[:/]\s*(\d{1,4})',
        r'([^\s،]+)\s+(\d{1,4})'  # last resort: e.g., "البقرة 5"
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized_text)
        if match:
            try:
                surah, ayah = int(match.group(1)), int(match.group(2))
                if 1 <= surah <= 114 and ayah >= 1:
                    logger.info(f"Local parse -> Surah {surah}, Ayah {ayah}")
                    return (surah, ayah)
            except Exception:
                continue
    return None


def identify_verse_with_gemini(text: str) -> Optional[Tuple[int, int]]:
    if not HAS_GEMINI or not gemini_client:
        logger.debug("Gemini not configured or unavailable")
        return None
    try:
        system_prompt = (
            "You are a Quranic verse identification expert. "
            "Analyze the given text and identify which surah (chapter) and ayah (verse) "
            "from the Quran is referenced. "
            "Return ONLY a JSON object with this exact format: "
            '{"surah": <number or null>, "ayah": <number or null>}. '
            "If you cannot identify a verse, return null for both fields."
        )
        # Using a generic content generation call — adjust per google-genai version if needed
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[types.Content(role="user", parts=[types.Part(text=text)])],
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=VerseIdentification,
            ),
        )
        if hasattr(response, 'text') and response.text:
            try:
                data = json.loads(response.text)
                verse_id = VerseIdentification(**data)
                if verse_id.surah and verse_id.ayah:
                    logger.info(f"Gemini parse -> Surah {verse_id.surah}, Ayah {verse_id.ayah}")
                    return (verse_id.surah, verse_id.ayah)
            except Exception as e:
                logger.error(f"Failed parsing Gemini response: {e}")
        return None
    except Exception as e:
        logger.error(f"Gemini identification failed: {e}")
        return None


def get_surah_info(surah: int) -> Optional[Dict]:
    try:
        url = f'{QURAN_API_BASE}/surah/{surah}'
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        # Support different Quran APIs that may use different keys
        if data.get('code') == 200 and 'data' in data:
            return data['data']
        # fallback common shape
        if 'data' in data and isinstance(data['data'], dict) and 'ayahs' in data['data']:
            return data['data']
        return None
    except Exception as e:
        logger.error(f"Failed to fetch surah info: {e}")
        return None


def compute_next_verse(current_surah: int, current_ayah: int) -> Tuple[int, int]:
    surah_info = get_surah_info(current_surah)
    if not surah_info:
        return (current_surah, current_ayah + 1)
    # try several keys for total ayahs
    total_ayahs = None
    for key in ('numberOfAyahs', 'number_of_ayahs', 'ayahs_count', 'numberOfAyahs'):
        if key in surah_info:
            try:
                total_ayahs = int(surah_info[key])
                break
            except Exception:
                continue
    # fallback if API returns list of ayahs
    if total_ayahs is None and isinstance(surah_info.get('ayahs'), list):
        total_ayahs = len(surah_info.get('ayahs'))
    if total_ayahs is None:
        return (current_surah, current_ayah + 1)
    if current_ayah < total_ayahs:
        return (current_surah, current_ayah + 1)
    else:
        return ((current_surah % 114) + 1, 1)


def fetch_verse(surah: int, ayah: int) -> Optional[Dict]:
    try:
        url = f'{QURAN_API_BASE}/ayah/{surah}:{ayah}'
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get('code') == 200 and 'data' in data:
            return data['data']
        # fallback: some APIs return {"text": ..., "surah": {...}}
        if 'text' in data and 'surah' in data:
            return data
        return None
    except Exception as e:
        logger.error(f"Failed to fetch verse: {e}")
        return None


def post_to_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials missing")
        return False
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
        payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        ok = response.json().get('ok', False)
        if not ok:
            logger.error(f"Telegram API returned not ok: {response.json()}")
        return ok
    except Exception as e:
        logger.error(f"Failed to post to Telegram: {e}")
        return False


def run_once():
    logger.info("="*60)
    logger.info("Starting Quran Verse Daily Poster")
    logger.info("="*60)

    run_log = {'timestamp': datetime.now().isoformat(), 'steps': [], 'success': False}

    # 1) Load last posted verse from state.json (single source of truth)
    state = load_state()
    current_surah = int(state.get('last_surah', 1))
    current_ayah = int(state.get('last_ayah', 0))
    run_log['steps'].append({'step': 'loaded_state', 'surah': current_surah, 'ayah': current_ayah})

    # 2) Try to read latest Telegram message and update current surah/ayah if message clearly indicates a different verse
    message = get_latest_telegram_message()
    if message:
        msg_text = None
        if 'text' in message:
            msg_text = message['text']
        elif 'caption' in message:
            msg_text = message['caption']
        elif 'photo' in message:
            # take highest resolution photo
            try:
                photo = message['photo'][-1]
                file_id = photo.get('file_id')
                if file_id:
                    msg_text = extract_text_from_image(file_id)
            except Exception:
                msg_text = None
        if msg_text:
            run_log['steps'].append({'step': 'telegram_message_text', 'text_preview': msg_text[:120]})
            verse_info = parse_verse_locally(msg_text)
            if not verse_info and HAS_GEMINI:
                verse_info = identify_verse_with_gemini(msg_text)
            if verse_info:
                # Update current to what telegram shows (but do NOT override state unless valid)
                current_surah, current_ayah = verse_info
                run_log['steps'].append({'step': 'updated_from_telegram', 'surah': current_surah, 'ayah': current_ayah})

    # 3) Compute next verse based on the current values
    next_surah, next_ayah = compute_next_verse(current_surah, current_ayah)
    run_log['steps'].append({'step': 'next_verse_computed', 'next_surah': next_surah, 'next_ayah': next_ayah})

    # 4) Fetch verse text
    verse_data = fetch_verse(next_surah, next_ayah)
    if not verse_data:
        run_log['steps'].append({'step': 'fetch_verse_failed'})
        save_run_log(run_log)
        logger.error("Stopping run due to fetch failure")
        return

    # normalize verse text and surah name from possible API shapes
    verse_text = verse_data.get('text') if isinstance(verse_data, dict) else None
    surah_name = None
    if isinstance(verse_data, dict):
        surah_info = verse_data.get('surah') or verse_data.get('surah')
        if isinstance(surah_info, dict):
            surah_name = surah_info.get('name')
    if not verse_text and 'text' in verse_data:
        verse_text = verse_data.get('text')
    if not surah_name:
        surah_name = f"Surah {next_surah}"

    message_to_post = f"<b>{surah_name} - آية {next_ayah}</b>\n\n{verse_text}"
    run_log['steps'].append({'step': 'prepared_message', 'preview': message_to_post[:120]})

    # 5) Post to Telegram
    posted = post_to_telegram(message_to_post)
    if posted:
        save_state(next_surah, next_ayah)
        run_log['success'] = True
        run_log['steps'].append({'step': 'posted_to_telegram', 'surah': next_surah, 'ayah': next_ayah})
        logger.info(f"✅ Posted Surah {next_surah} Ayah {next_ayah}")
    else:
        run_log['steps'].append({'step': 'telegram_post_failed'})
        logger.error("Failed to post to telegram")

    # 6) Save run log
    save_run_log(run_log)
    logger.info("="*60)
    logger.info("Run completed")
    logger.info("="*60)


if __name__ == '__main__':
    run_once()
