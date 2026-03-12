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
OPENROUTER_API_KEY = os.getenv("OPENROUTER_KEY")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Validate environment variables
missing_vars = []
if not TELEGRAM_TOKEN:
    missing_vars.append("TELEGRAM_BOT_TOKEN")
if not OPENROUTER_API_KEY:
    missing_vars.append("OPENROUTER_KEY")
if not CHAT_ID:
    missing_vars.append("TELEGRAM_CHAT_ID")
if missing_vars:
    raise EnvironmentError(f"Missing environment variables: {', '.join(missing_vars)}")

HISTORY_FILE = "posted_links.txt"
REQUEST_TIMEOUT = 15
RATE_LIMIT_SLEEP = 2
MAX_ITEMS_PER_SOURCE = 5  # Check up to 5 headlines per source

# Moldova-focused RSS feeds
SOURCES = {
    "TV8 Moldova": "https://tv8.md/feed",
    "Ziarul de Gardă": "https://www.zdg.md/feed",
    "Newsmaker MD": "https://newsmaker.md/feed"
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
    Use OpenRouter API to summarize political news.
    OpenRouter is free and works reliably from GitHub Actions.
    """
    url = "https://openrouter.ai/api/v1/chat/completions"

    prompt = (
        f"Geopolitical analysis for a Moldova news channel. News from {source}: {title}. "
        f"Summarize in one sharp sentence in English. If the news is not political "
        f"(e.g., sports, entertainment, gossip), reply with exactly 'IGNORE'."
    )

    payload = {
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "max_tokens": 150,
        "temperature": 0.5
    }

    data = json.dumps(payload).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {OPENROUTER_API_KEY}',
        'HTTP-Referer': 'https://github.com',
        'X-Title': 'Moldova News Bot'
    }

    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            res = json.loads(response.read().decode('utf-8'))
            text = res['choices'][0]['message']['content'].strip()
            return None if text == "IGNORE" else text
    except Exception as e:
        logging.warning(f"OpenRouter API error for {source}: {e}")
        return None

def escape_markdown(text: str) -> str:
    special_chars = r'_*[]()~`>#+-=|{}.!'
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

def post_to_telegram(source_name: str, analysis: str, link: str) -> None:
    """
    Send message to Telegram.
    Fixed: text and link are escaped separately so the link stays clickable.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    safe_source = escape_markdown(source_name)
    safe_analysis = escape_markdown(analysis)

    message = f"🇲🇩 *Moldova News* – {safe_source}\n\n{safe_analysis}\n\n🔗 [Read article]({link})"

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
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

def get_link_from_item(item) -> Optional[str]:
    """
    Safely extract link from RSS item.
    Tries multiple locations since different feeds store links differently.
    """
    # Try standard <link> tag first
    link_elem = item.find('link')
    if link_elem is not None and link_elem.text and link_elem.text.startswith('http'):
        return link_elem.text.strip()

    # Some feeds put it as text after the <link> tag
    if link_elem is not None and link_elem.tail and link_elem.tail.strip().startswith('http'):
        return link_elem.tail.strip()

    # Try <guid> tag which often contains the URL
    guid_elem = item.find('guid')
    if guid_elem is not None and guid_elem.text and guid_elem.text.startswith('http'):
        return guid_elem.text.strip()

    return None

def fetch_rss_items(source_name: str, feed_url: str) -> list:
    """Fetch up to MAX_ITEMS_PER_SOURCE items from an RSS feed."""
    items = []
    try:
        req = urllib.request.Request(feed_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            root = ET.fromstring(response.read())

            all_items = root.findall('.//item')
            for item in all_items[:MAX_ITEMS_PER_SOURCE]:
                link = get_link_from_item(item)
                title_elem = item.find('title')

                if link and title_elem is not None and title_elem.text:
                    items.append((link, title_elem.text.strip()))

    except Exception as e:
        logging.error(f"Failed to fetch RSS for {source_name}: {e}")
    return items

def run() -> None:
    history = load_history()
    logging.info(f"Loaded {len(history)} previously posted links.")

    for source_name, feed_url in SOURCES.items():
        logging.info(f"Processing {source_name}...")
        items = fetch_rss_items(source_name, feed_url)
        logging.info(f"Found {len(items)} items from {source_name}")

        for link, title in items:
            if link in history:
                logging.debug(f"Skipping already posted link: {link}")
                continue

            analysis = ask_ai_geopolitics(title, source_name)
            if analysis is None:
                logging.info(f"Skipped '{title}' – AI marked as IGNORE or API failed.")
                save_history(link)  # Remember it so we don't retry non-political articles
                history.add(link)
                continue

            post_to_telegram(source_name, analysis, link)
            save_history(link)
            history.add(link)
            logging.info(f"Posted: {title[:50]}...")
            time.sleep(RATE_LIMIT_SLEEP)

        time.sleep(RATE_LIMIT_SLEEP)

if __name__ == "__main__":
    run()
