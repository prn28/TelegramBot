import urllib.request
import urllib.parse
import json
import xml.etree.ElementTree as ET
import time
import os
import logging
from typing import Set, Optional

# --- 🔐 SECURE CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Validate environment variables
missing_vars = []
if not TELEGRAM_TOKEN:
    missing_vars.append("TELEGRAM_BOT_TOKEN")
if not GEMINI_API_KEY:
    missing_vars.append("GEMINI_API_KEY")
if not CHAT_ID:
    missing_vars.append("TELEGRAM_CHAT_ID")
if missing_vars:
    raise EnvironmentError(f"Missing environment variables: {', '.join(missing_vars)}")

HISTORY_FILE = "posted_links.txt"
REQUEST_TIMEOUT = 15
RATE_LIMIT_SLEEP = 2

# Politics-focused RSS feeds
SOURCES = {
    "BBC Politics": "http://newsrss.bbc.co.uk/rss/newsonline_uk_edition/uk_politics/rss.xml",
    "Fox News Politics": "https://moxie.foxnews.com/google-publisher/politics.xml",
    "CNN Politics": "http://rss.cnn.com/rss/cnn_allpolitics.rss"
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def load_history() -> Set[str]:
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_history(link: str) -> None:
    with open(HISTORY_FILE, "a") as f:
        f.write(link + "\n")

def ask_ai_geopolitics(title: str, source: str) -> Optional[str]:
    """
    Use Gemini API to summarize political news.
    The critical fix: using '/models/' endpoint without version prefix.
    """
    # Use the model name that is confirmed to work (gemini-2.5-flash)
    model_name = "gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/models/{model_name}:generateContent?key={GEMINI_API_KEY}"

    prompt = (
        f"Geopolitical analysis for Kamorka channel. News from {source}: {title}. "
        f"Summarize in one sharp sentence. If the news is not political "
        f"(e.g., sports, entertainment, gossip), reply with exactly 'IGNORE'."
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    data = json.dumps(payload).encode('utf-8')
    headers = {'Content-Type': 'application/json'}

    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            res = json.loads(response.read().decode('utf-8'))
            text = res['candidates'][0]['content']['parts'][0]['text'].strip()
            return None if text == "IGNORE" else text
    except Exception as e:
        logging.warning(f"Gemini API error for {source}: {e}")
        # Uncomment the next line to see full traceback (useful for debugging)
        # logging.exception("Detailed error:")
        return None

def escape_markdown(text: str) -> str:
    special_chars = r'_*[]()~`>#+-=|{}.!'
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

def post_to_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    safe_text = escape_markdown(text)
    payload = {
        "chat_id": CHAT_ID,
        "text": safe_text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": False
    }
    data = json.dumps(payload).encode('utf-8')
    headers = {'Content-Type': 'application/json'}

    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            logging.info("Message posted successfully")
    except Exception as e:
        logging.error(f"Failed to post to Telegram: {e}")

def fetch_rss_items(source_name: str, feed_url: str) -> list:
    """Fetch the latest item from an RSS feed."""
    items = []
    try:
        req = urllib.request.Request(feed_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            root = ET.fromstring(response.read())
            item = root.find('.//item')
            if item is not None:
                link_elem = item.find('link')
                title_elem = item.find('title')
                if link_elem is not None and title_elem is not None:
                    items.append((link_elem.text, title_elem.text))
    except Exception as e:
        logging.error(f"Failed to fetch RSS for {source_name}: {e}")
    return items

def run() -> None:
    history = load_history()
    logging.info(f"Loaded {len(history)} previously posted links.")

    for source_name, feed_url in SOURCES.items():
        logging.info(f"Processing {source_name}...")
        items = fetch_rss_items(source_name, feed_url)

        for link, title in items:
            if link in history:
                logging.debug(f"Skipping already posted link: {link}")
                continue

            analysis = ask_ai_geopolitics(title, source_name)
            if analysis is None:
                logging.info(f"Skipped '{title}' – AI marked as IGNORE or API failed.")
                continue

            message = f"🏛️ *Kamorka Alert* – {source_name}\n\n{analysis}\n\n🔗 [Link]({link})"
            post_to_telegram(message)
            save_history(link)
            history.add(link)
            logging.info(f"Posted: {title[:50]}...")
            time.sleep(RATE_LIMIT_SLEEP)

        time.sleep(RATE_LIMIT_SLEEP)

if __name__ == "__main__":
    run()
