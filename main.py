import urllib.request
import json
import xml.etree.ElementTree as ET
import time
import os
import re
import logging
from typing import Set, Optional, List

# --- CONFIG ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

HISTORY_FILE = "posted_links.txt"
TITLE_HISTORY_FILE = "posted_titles.txt"

REQUEST_TIMEOUT = 15
RATE_LIMIT_SLEEP = 3
MAX_ITEMS_PER_SOURCE = 5

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL_NAME = "google/gemini-2.0-flash-001"

logging.basicConfig(level=logging.INFO)

SOURCES = {
    "TV8 Moldova": "https://tv8.md/feed",
    "Ziarul de Gardă": "https://www.zdg.md/feed",
    "Newsmaker MD": "https://newsmaker.md/feed",
    "Realitatea.md": "https://realitatea.md/rss"
}

# --- HELPERS ---

def normalize_title(title: str) -> str:
    title = title.lower()
    title = re.sub(r'sursa[:\-].*', '', title)
    title = re.sub(r'[^\w\s]', '', title)
    return re.sub(r'\s+', ' ', title).strip()

def is_repost(title: str) -> bool:
    t = title.lower()
    return "sursa:" in t or "source:" in t or "preluat" in t

def load_history() -> Set[str]:
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return set(f.read().splitlines())
    return set()

def save_to_history(link: str):
    with open(HISTORY_FILE, "a") as f:
        f.write(link + "\n")

def load_title_history() -> List[str]:
    if os.path.exists(TITLE_HISTORY_FILE):
        with open(TITLE_HISTORY_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        return lines[-200:]
    return []

def save_title_history(title: str):
    with open(TITLE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(normalize_title(title) + "\n")

# --- AI LOGIC ---

def call_ai(prompt: str, max_tokens: int = 150) -> Optional[str]:
    if not OPENROUTER_API_KEY:
        logging.error("API Key is missing!")
        return None

    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2
    }
    try:
        req = urllib.request.Request(
            OPENROUTER_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "http://localhost",
            }
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as res:
            data = json.loads(res.read())
            content = data.get("choices", [{}])[0].get("message", {}).get("content")
            return content.strip() if content else None
    except Exception as e:
        logging.error(f"OpenRouter API error: {e}")
        return None

def ask_ai_filter_and_summarize(title: str) -> Optional[str]:
    prompt = f"Ești editor știri Moldova. Titlu: \"{title}\". Permite doar impact major. Dacă e minor: IGNORE. Dacă e major, răspunde doar JSON: {{\"ro\": \"rezumat 1 propoziție\"}}"
    text = call_ai(prompt)
    if not text or "IGNORE" in text.upper():
        return None
    try:
        text = re.sub(r"```[a-z]*|```", "", text).strip()
        return json.loads(text).get("ro")
    except:
        logging.error(f"AI filter parse error: {text}")
        return None

def is_same_event(new_title: str, past_titles: List[str]) -> bool:
    if not past_titles:
        return False
    prompt = f"Titlu nou: \"{new_title}\"\nȘtiri recente: {past_titles[-15:]}\nEste același eveniment? Răspunde doar YES sau NO."
    answer = call_ai(prompt, max_tokens=10)
    return answer and "YES" in answer.upper()

# --- RSS ---

def fetch_rss_items(feed_url: str):
    items = []
    try:
        req = urllib.request.Request(feed_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            root = ET.fromstring(response.read())
            for item in root.findall('.//item')[:MAX_ITEMS_PER_SOURCE]:
                title_el = item.find('title')
                link_el = item.find('link')
                if title_el is None or link_el is None:
                    continue
                title = title_el.text.strip()
                link = link_el.text.strip()
                items.append((link, title))
    except Exception as e:
        logging.error(f"RSS error: {e}")
    return items

# --- TELEGRAM ---

def post_to_telegram(source: str, summary: str, link: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    message = f"🇲🇩 <b>{source}</b>\n\n{summary}\n\n🔗 <a href='{link}'>Citește articolul</a>"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req)
        logging.info(f"Posted successfully: {source}")
    except Exception as e:
        logging.error(f"Telegram error: {e}")

# --- MAIN ---

def run():
    seen_links = load_history()
    seen_titles = load_title_history()
    for source, feed in SOURCES.items():
        logging.info(f"Checking {source}...")
        items = fetch_rss_items(feed)
        logging.info(f"Found {len(items)} items from {source}")
        for link, title in items:
            logging.info(f"Processing: {title}")
            if is_repost(title):
                logging.info("Skipped: repost")
                continue
            if link in seen_links:
                logging.info("Skipped: already posted")
                continue

            summary = ask_ai_filter_and_summarize(title)
            logging.info(f"AI summary result: {summary}")
            if not summary:
                continue
            if is_same_event(title, seen_titles):
                logging.info("Skipped: same event")
                continue

            post_to_telegram(source, summary, link)
            logging.info(f"Posted: {title}")
            save_to_history(link)
            save_title_history(title)
            seen_links.add(link)
            seen_titles.append(normalize_title(title))
            time.sleep(RATE_LIMIT_SLEEP)


if __name__ == "__main__":
    while True:
        logging.info("Starting news cycle...")
        run()
        logging.info("Sleeping for 30 minutes...")
        time.sleep(30 * 60)
