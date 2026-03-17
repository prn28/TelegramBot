import urllib.request
import json
import xml.etree.ElementTree as ET
import time
import os
import re
import logging
from typing import Set, Optional, List


TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# Synchronized to match your GitHub Secret name
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
    prompt = f"Ești editor știri. Titlu: \"{title}\". Permite doar impact major (politică, economie, crize). Dacă e minor: IGNORE. Dacă e major, răspunde doar JSON: {{\"ro\": \"rezumat 1 propoziție\"}}"
    text = call_ai(prompt)
    if not text or "IGNORE" in text.upper():
        return None
    try:
        text = re.sub(r"```[a-z]*|```", "", text).strip()
        return json.loads(text).get("ro")
    except:
        return None

def is_same_event(new_title: str, past_titles: List[str]) -> bool:
    if not past_titles: return False
    prompt = f"Titlu nou: \"{new_title}\"\nȘtiri vechi: {past_titles[-15:]}\nEste același eveniment? Răspunde doar YES sau NO."
    answer = call_ai(prompt, max_tokens=10)
    return answer and "YES" in answer.upper()

# --- PROCESSING ---

def fetch_rss_items(feed_url: str):
    items = []
    try:
        req = urllib.request.Request(feed_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            root = ET.fromstring(response.read())
            for item in root.findall('.//item')[:MAX_ITEMS_PER_SOURCE]:
                title = item.find('title').text.strip()
                link = item.find('link').text.strip()
                items.append((link, title))
    except Exception as e:
        logging.error(f"RSS error: {e}")
    return items

def post_to_telegram(source: str, summary: str, link: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    message = f"🇲🇩 <b>{source}</b>\n\n{summary}\n\n🔗 <a href='{link}'>Citește articolul</a>"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req)
    except Exception as e:
        logging.error(f"Telegram error: {e}")

def run():
    seen_links = load_history()
    seen_titles = load_title_history()
    for source, feed in SOURCES.items():
        for link, title in fetch_rss_items(feed):
            if is_repost(title) or link in seen_links: continue
            summary = ask_ai_filter_and_summarize(title)
            if not summary or is_same_event(title, seen_titles): continue
            
            post_to_telegram(source, summary, link)
            save_to_history(link)
            save_title_history(title)
            seen_links.add(link)
            seen_titles.append(normalize_title(title))
            time.sleep(RATE_LIMIT_SLEEP)

if __name__ == "__main__":
    run()
