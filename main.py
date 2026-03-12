import urllib.request
import urllib.parse
import json
import xml.etree.ElementTree as ET
import time
import os
import logging
from typing import Set, Optional

# --- 🔐 SECURE CONFIGURATION ---
# Read tokens from environment variables (set in GitHub Secrets)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Debug: show which variables are missing
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
REQUEST_TIMEOUT = 15          # seconds
RATE_LIMIT_SLEEP = 2          # seconds between API calls
MAX_ITEMS_PER_SOURCE = 5      # how many headlines to check per source

# FIX 1: Updated RSS feed URLs (BBC old URL was dead)
SOURCES = {
    "BBC Politics": "https://feeds.bbci.co.uk/news/politics/rss.xml",
    "Fox News Politics": "https://moxie.foxnews.com/google-publisher/politics.xml",
    "CNN Politics": "http://rss.cnn.com/rss/cnn_allpolitics.rss"
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def load_history() -> Set[str]:
    """Load already posted links from history file."""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_history(link: str) -> None:
    """Append a new link to the history file."""
    with open(HISTORY_FILE, "a") as f:
        f.write(link + "\n")

def ask_ai_geopolitics(title: str, source: str) -> Optional[str]:
    """
    Send title to Gemini API and request a one-sentence geopolitical summary.
    Returns None if the news should be ignored (non-political) or if the API call fails.
    """
    # FIX 2: Updated to a stable Gemini model name (not a preview that may expire)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
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
        return None

def escape_markdown(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters (but NOT inside URLs)."""
    special_chars = r'_*[]()~`>#+-=|{}.!'
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

def post_to_telegram(title_line: str, analysis: str, link: str) -> None:
    """
    Send a Markdown-formatted message to the Telegram channel.
    
    FIX 3: We now escape only the text parts separately, and keep the
    Markdown link syntax [text](url) intact so Telegram renders it correctly.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    # Escape only the text portions, NOT the link itself
    safe_title = escape_markdown(title_line)
    safe_analysis = escape_markdown(analysis)

    # Build message with link syntax kept separate from escaping
    message = f"🏛️ *Kamorka Alert* – {safe_title}\n\n{safe_analysis}\n\n🔗 [Read article]({link})"

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
    FIX 4: Safely extract link from RSS item.
    Some feeds (e.g. CNN) put the URL in different places, so we try multiple spots.
    """
    # Try standard <link> tag first
    link_elem = item.find('link')
    if link_elem is not None and link_elem.text and link_elem.text.startswith('http'):
        return link_elem.text.strip()

    # Some feeds put it as text after the <link> tag (common in Atom-style RSS)
    if link_elem is not None and link_elem.tail and link_elem.tail.strip().startswith('http'):
        return link_elem.tail.strip()

    # Try <guid> tag which often contains the URL
    guid_elem = item.find('guid')
    if guid_elem is not None and guid_elem.text and guid_elem.text.startswith('http'):
        return guid_elem.text.strip()

    return None  # Could not find a valid link

def fetch_rss_items(source_name: str, feed_url: str) -> list:
    """
    FIX 5: Fetch multiple items (up to MAX_ITEMS_PER_SOURCE) instead of just one.
    Previously only the very first headline was ever checked.
    """
    items = []
    try:
        req = urllib.request.Request(feed_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            root = ET.fromstring(response.read())

            # Get multiple recent items, not just the first one
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
                save_history(link)   # Remember it so we don't keep retrying non-political articles
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
