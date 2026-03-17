import urllib.request
import urllib.parse
import json
import xml.etree.ElementTree as ET
import time
import os
import re
import logging
from typing import Set, Optional, List, Tuple

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
TITLE_HISTORY_FILE = "posted_titles.txt"
TITLE_SIMILARITY_THRESHOLD = 0.75
REQUEST_TIMEOUT = 15
RATE_LIMIT_SLEEP = 2
MAX_ITEMS_PER_SOURCE = 5
AI_RETRY_ATTEMPTS = 3
AI_RETRY_DELAY = 10

# --- ⏱ SELF-LOOP CONFIGURATION ---
LOOP_INTERVAL_MINUTES = 30
JOB_DURATION_HOURS = 5.5

# Moldova-focused RSS feeds
SOURCES = {
    "TV8 Moldova": "https://tv8.md/feed",
    "Ziarul de Gardă": "https://www.zdg.md/feed",
    "Newsmaker MD": "https://newsmaker.md/feed"
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# ---------------------------------------------------------------------------
# 🔁 DEDUPLICATION HELPERS
# ---------------------------------------------------------------------------

def normalize_title(title: str) -> str:
    title = title.lower()
    title = re.sub(r'[^\w\s]', '', title)
    title = re.sub(r'\s+', ' ', title).strip()
    return title

def title_similarity(a: str, b: str) -> float:
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)

def load_history() -> Set[str]:
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_to_history(link: str) -> None:
    with open(HISTORY_FILE, "a") as f:
        f.write(link + "\n")

def load_title_history() -> List[str]:
    if os.path.exists(TITLE_HISTORY_FILE):
        with open(TITLE_HISTORY_FILE, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    return []

def save_title_history(title: str) -> None:
    with open(TITLE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(normalize_title(title) + "\n")

def is_duplicate_title(title: str, seen_titles: List[str]) -> bool:
    norm = normalize_title(title)
    for seen in seen_titles:
        if title_similarity(norm, seen) >= TITLE_SIMILARITY_THRESHOLD:
            return True
    return False


# ---------------------------------------------------------------------------
# 🤖 AI FILTERING & SUMMARIZATION
# ---------------------------------------------------------------------------

def ask_ai_geopolitics(title: str, source: str) -> Optional[dict]:
    """
    Returns a dict {"ro": "...", "en": "..."} for political news,
    or None if not political or API failed.
    Retries automatically on 429.
    """
    url = "https://openrouter.ai/api/v1/chat/completions"

    prompt = "\n".join([
        f'You are an editor for a Moldovan news channel.',
        f'Analyze this news headline from {source}: "{title}"',
        '',
        'If this is NOT political or geopolitical news (e.g. sports, entertainment, celebrity gossip, weather),',
        'respond with exactly: IGNORE',
        '',
        'If it IS political/geopolitical, respond with a JSON object and nothing else',
        '(no markdown, no backticks, no explanation — raw JSON only):',
        '{"ro": "2-3 sentence clear description in Romanian", "en": "2-3 sentence clear description in English"}',
        '',
        'Each description must explain: what happened, who is involved, and why it matters.',
        'Write in a neutral, journalistic tone. Do not start with the source name.',
    ])

    payload = {
        "model": "openrouter/auto",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
        "temperature": 0.4
    }

    data = json.dumps(payload).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {OPENROUTER_API_KEY}',
        'HTTP-Referer': 'https://github.com',
        'X-Title': 'Moldova News Bot'
    }

    for attempt in range(1, AI_RETRY_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read().decode('utf-8')
                res = json.loads(raw)
                text = res['choices'][0]['message']['content'].strip()

                if text.upper() == "IGNORE":
                    return None

                # Parse the JSON response from the AI
                try:
                    parsed = json.loads(text)
                    if "ro" in parsed and "en" in parsed:
                        return parsed
                    else:
                        logging.warning(f"AI returned unexpected JSON keys: {text[:100]}")
                        return None
                except json.JSONDecodeError:
                    logging.warning(f"AI did not return valid JSON: {text[:100]}")
                    return None

        except urllib.error.HTTPError as e:
            if e.code == 429:
                logging.warning(f"OpenRouter rate-limited (429) for '{title}' – attempt {attempt}/{AI_RETRY_ATTEMPTS}. Waiting {AI_RETRY_DELAY}s...")
                if attempt < AI_RETRY_ATTEMPTS:
                    time.sleep(AI_RETRY_DELAY)
                else:
                    logging.warning(f"All {AI_RETRY_ATTEMPTS} attempts exhausted for '{title}'. Skipping.")
                    return None
            else:
                logging.warning(f"OpenRouter HTTP error {e.code} for {source}: {e}")
                return None

        except Exception as e:
            logging.warning(f"OpenRouter API error for {source}: {e}")
            return None

    return None


# ---------------------------------------------------------------------------
# 📨 TELEGRAM
# ---------------------------------------------------------------------------

def escape_markdown(text: str) -> str:
    special_chars = r'_*[]()~`>#+-=|{}.!'
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

def post_to_telegram(source_name: str, analysis: dict, link: str) -> None:
    """
    Message format:
    🇲🇩 Republica News – [Source]

    [Romanian description]

    ———————————————

    [English description]

    🔗 Read article
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    safe_source = escape_markdown(source_name)
    safe_ro = escape_markdown(analysis["ro"])
    safe_en = escape_markdown(analysis["en"])
    divider = escape_markdown("———————————————")

    message = (
        f"🇲🇩 *Republica News* \\– {safe_source}\n\n"
        f"{safe_ro}\n\n"
        f"{divider}\n\n"
        f"{safe_en}\n\n"
        f"🔗 [Citește articolul]({link})"
    )

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


# ---------------------------------------------------------------------------
# 📡 RSS FETCHING
# ---------------------------------------------------------------------------

def get_link_from_item(item) -> Optional[str]:
    link_elem = item.find('link')
    if link_elem is not None and link_elem.text and link_elem.text.startswith('http'):
        return link_elem.text.strip()
    if link_elem is not None and link_elem.tail and link_elem.tail.strip().startswith('http'):
        return link_elem.tail.strip()
    guid_elem = item.find('guid')
    if guid_elem is not None and guid_elem.text and guid_elem.text.startswith('http'):
        return guid_elem.text.strip()
    return None

def fetch_rss_items(source_name: str, feed_url: str) -> List[Tuple[str, str]]:
    items = []
    try:
        req = urllib.request.Request(feed_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            root = ET.fromstring(response.read())
            for item in root.findall('.//item')[:MAX_ITEMS_PER_SOURCE]:
                link = get_link_from_item(item)
                title_elem = item.find('title')
                if link and title_elem is not None and title_elem.text:
                    items.append((link, title_elem.text.strip()))
    except Exception as e:
        logging.error(f"Failed to fetch RSS for {source_name}: {e}")
    return items


# ---------------------------------------------------------------------------
# 🚀 MAIN CYCLE
# ---------------------------------------------------------------------------

def run() -> None:
    url_history = load_history()
    title_history = load_title_history()
    logging.info(f"Loaded {len(url_history)} posted URLs, {len(title_history)} posted titles.")

    for source_name, feed_url in SOURCES.items():
        logging.info(f"Processing {source_name}...")
        items = fetch_rss_items(source_name, feed_url)
        logging.info(f"Found {len(items)} items from {source_name}")

        for link, title in items:
            # 1. Skip if URL already posted
            if link in url_history:
                logging.debug(f"Skipping already posted URL: {link}")
                continue

            # 2. Skip if same story already posted (different source or slightly different URL)
            if is_duplicate_title(title, title_history):
                logging.info(f"Skipping duplicate story: '{title[:60]}'")
                url_history.add(link)
                save_to_history(link)
                continue

            analysis = ask_ai_geopolitics(title, source_name)
            if analysis is None:
                logging.info(f"Skipped '{title}' – AI marked as IGNORE or returned invalid response.")
                url_history.add(link)
                save_to_history(link)
                title_history.append(normalize_title(title))
                save_title_history(title)
                continue

            post_to_telegram(source_name, analysis, link)
            url_history.add(link)
            save_to_history(link)
            title_history.append(normalize_title(title))
            save_title_history(title)
            logging.info(f"Posted: {title[:50]}...")
            time.sleep(RATE_LIMIT_SLEEP)

        time.sleep(RATE_LIMIT_SLEEP)


if __name__ == "__main__":
    import datetime
    job_start = time.time()
    job_end = job_start + JOB_DURATION_HOURS * 3600
    cycle = 1

    while True:
        logging.info(f"--- Cycle {cycle} starting at {datetime.datetime.utcnow().strftime('%H:%M:%S')} UTC ---")
        run()
        cycle += 1

        now = time.time()
        next_run = now + LOOP_INTERVAL_MINUTES * 60

        if next_run >= job_end:
            logging.info("Approaching GitHub Actions time limit — exiting cleanly.")
            break

        sleep_secs = next_run - now
        logging.info(f"Sleeping {LOOP_INTERVAL_MINUTES} minutes until next cycle...")
        time.sleep(sleep_secs)
