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
OPEN_ROUTER_API_KEY = os.getenv("OPEN_ROUTER_API_KEY")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

HISTORY_FILE = "posted_links.txt"
TITLE_HISTORY_FILE = "posted_titles.txt"

REQUEST_TIMEOUT = 15
RATE_LIMIT_SLEEP = 2
MAX_ITEMS_PER_SOURCE = 5

logging.basicConfig(level=logging.INFO)

SOURCES = {
    "TV8 Moldova": "https://tv8.md/feed",
    "Ziarul de Gardă": "https://www.zdg.md/feed",
    "Newsmaker MD": "https://newsmaker.md/feed",
    "Realitatea.md": "https://realitatea.md/rss"
}

# ---------------------------------------------------------------------------
# 🧠 TITLE HELPERS
# ---------------------------------------------------------------------------

def normalize_title(title: str) -> str:
    title = title.lower()
    title = re.sub(r'sursa[:\-].*', '', title)
    title = re.sub(r'[^\w\s]', '', title)
    return re.sub(r'\s+', ' ', title).strip()

def is_repost(title: str) -> bool:
    t = title.lower()
    return "sursa:" in t or "source:" in t or "preluat" in t

# ---------------------------------------------------------------------------
# 📁 HISTORY
# ---------------------------------------------------------------------------

def load_history() -> Set[str]:
    if os.path.exists(HISTORY_FILE):
        return set(open(HISTORY_FILE).read().splitlines())
    return set()

def save_to_history(link: str):
    with open(HISTORY_FILE, "a") as f:
        f.write(link + "\n")

def load_title_history() -> List[str]:
    if os.path.exists(TITLE_HISTORY_FILE):
        return open(TITLE_HISTORY_FILE, encoding="utf-8").read().splitlines()
    return []

def save_title_history(title: str):
    with open(TITLE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(normalize_title(title) + "\n")

# ---------------------------------------------------------------------------
# 🤖 AI: FILTER + SUMMARY
# ---------------------------------------------------------------------------

def ask_ai_filter_and_summarize(title: str) -> Optional[str]:
    url = "https://openrouter.ai/api/v1/chat/completions"

    prompt = f"""
Ești editor pentru un canal de știri foarte selectiv.

Titlu: "{title}"

Permite DOAR știri cu impact major:
- decizii guvernamentale
- politică națională/internațională
- conflicte, crize, economie majoră

Respinge:
- știri minore
- opinii
- evenimente locale nesemnificative

Dacă NU este important: răspunde IGNORE

Dacă ESTE:
Răspunde DOAR:
{{"ro": "rezumat foarte scurt, 1 propoziție"}}
"""

    payload = {
        "model": "openrouter/auto",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 80
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {OPEN_ROUTER_API_KEY}",
                "Content-Type": "application/json"
            }
        )

        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as res:
            data = json.loads(res.read())
            text = data["choices"][0]["message"]["content"].strip()

            if text.upper() == "IGNORE":
                return None

            parsed = json.loads(text)
            return parsed.get("ro")

    except Exception as e:
        logging.error(f"AI filter error: {e}")
        return None

# ---------------------------------------------------------------------------
# 🤖 AI: SAME EVENT DETECTION
# ---------------------------------------------------------------------------

def is_same_event(new_title: str, past_titles: List[str]) -> bool:
    if not past_titles:
        return False

    url = "https://openrouter.ai/api/v1/chat/completions"
    recent = past_titles[-20:]

    prompt = f"""
Titlu nou:
"{new_title}"

Știri existente:
{chr(10).join(recent)}

Este același eveniment?

Răspunde DOAR: YES sau NO
"""

    payload = {
        "model": "openrouter/auto",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 5
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {OPEN_ROUTER_API_KEY}",
                "Content-Type": "application/json"
            }
        )

        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as res:
            data = json.loads(res.read())
            answer = data["choices"][0]["message"]["content"].strip().upper()
            return "YES" in answer

    except Exception as e:
        logging.error(f"AI dedup error: {e}")
        return False

# ---------------------------------------------------------------------------
# 📡 RSS
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# 📲 TELEGRAM (FIXED HTML)
# ---------------------------------------------------------------------------

def post_to_telegram(source: str, summary: str, link: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    message = (
        f"🇲🇩 <b>Republica News</b> – {source}\n\n"
        f"{summary}\n\n"
        f"🔗 <a href='{link}'>Citește articolul</a>"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req)
        logging.info("Posted successfully")

    except Exception as e:
        logging.error(f"Telegram error: {e}")

# ---------------------------------------------------------------------------
# 🚀 MAIN
# ---------------------------------------------------------------------------

def run():
    seen_links = load_history()
    seen_titles = load_title_history()

    for source, feed in SOURCES.items():
        items = fetch_rss_items(feed)

        for link, title in items:

            if is_repost(title):
                continue

            if link in seen_links:
                continue

            if is_same_event(title, seen_titles):
                continue

            summary = ask_ai_filter_and_summarize(title)
            if not summary:
                continue

            post_to_telegram(source, summary, link)

            save_to_history(link)
            save_title_history(title)

            seen_links.add(link)
            seen_titles.append(normalize_title(title))

            time.sleep(RATE_LIMIT_SLEEP)

        time.sleep(RATE_LIMIT_SLEEP)

if __name__ == "__main__":
    run()
