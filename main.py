import urllib.request
import json
import xml.etree.ElementTree as ET
import time
import os
import re
import logging
from typing import Set, Optional, List, Tuple

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPEN_ROUTER_API_KEY = os.getenv("OPEN_ROUTER_API_KEY")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

HISTORY_FILE = "posted_links.txt"
TITLE_HISTORY_FILE = "posted_titles.txt"

REQUEST_TIMEOUT = 15
RATE_LIMIT_SLEEP = 2
MAX_ITEMS_PER_SOURCE = 5

logging.basicConfig(level=logging.INFO)

# ========== SOURCES ==========
SOURCES = {
    # Your existing sources
    "TV8 Moldova": "https://tv8.md/feed",
    "Ziarul de Gardă": "https://www.zdg.md/feed",
    "Newsmaker MD": "https://newsmaker.md/feed",
    "Realitatea.md": "https://realitatea.md/rss",
    "MOLDPRES - Română": "https://moldpres.md/config/rss.php?lang=rom",
    "MOLDPRES - Русский": "https://moldpres.md/config/rss.php?lang=rus",
    "MOLDPRES - English": "https://moldpres.md/config/rss.php?lang=eng",
    "MOLDPRES Sinteza - Română": "https://moldpres.md/config/rssSinteza.php?lang=rom",
    "Stiri.md": "https://stiri.md/rss",  # Comprehensive news portal [citation:9]
    "Canal Regional TV": "https://canalregionaltv.md/feed",  # Regional news [citation:8]
    "Media TV": "https://mediatv.md/feed",  # News outlet [citation:8]
    "MOVCA": "https://movca.md/feed",  # News/cultural content [citation:8]
    "ASE MD": "https://ase.md/feed",  # Economic news (university, but may have econ content) [citation:8]
    "Biofood": "https://biofood.md/feed",  # Agriculture/food news [citation:8]
}

# ========== SOURCE TEMPLATES ==========
SOURCE_TEMPLATES = {
    "TV8 Moldova": (
        "📺 <b>TV8 Moldova</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Vezi pe TV8.md</a>"
    ),
    "Ziarul de Gardă": (
        "📰 <b>Ziarul de Gardă</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Continuă pe ZDG.md</a>"
    ),
    "Newsmaker MD": (
        "📢 <b>Newsmaker MD</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Citește integral</a>"
    ),
    "Realitatea.md": (
        "📡 <b>Realitatea.md</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Vezi știrea</a>"
    ),
    "MOLDPRES - Română": (
        "🏛️ <b>MOLDPRES</b> – Agenția de Stat\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Sursa oficială</a>"
    ),
    ),
    ),
    ),
    "Stiri.md": (
        "📌 <b>Stiri.md</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Citește pe Stiri.md</a>"
    ),
    "Canal Regional TV": (
        "📺 <b>Canal Regional TV</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Vezi știrea:</a>"
    ),
    "Media TV": (
        "📡 <b>Media TV</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Continuă să citești:</a>"
    ),
    "MOVCA": (
        "🎭 <b>MOVCA</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Detalii:</a>"
    ),
    "ASE MD": (
        "📊 <b>ASE Moldova</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Citește mai jos:</a>"
    ),
    "Biofood": (
        "🌱 <b>Biofood</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Citește mai jos:</a>"
    ),
}
DEFAULT_TEMPLATE = "{summary}\n\n🔗 <a href='{link}'>Citește articolul</a>"

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
# 🤖 AI: FILTER + SUMMARY (using title + description)
# ---------------------------------------------------------------------------

def ask_ai_filter_and_summarize(title: str, description: str = "") -> Optional[str]:
    url = "https://openrouter.ai/api/v1/chat/completions"

    # Build the input text: title + description (if available)
    content = f"Titlu: {title}\n"
    if description:
        content += f"Descriere: {description}\n"

    prompt = f"""
Ești editor pentru un canal de știri foarte selectiv.

{content}

Categorii permise:
- politică națională și internațională (decizii guvernamentale, alegeri, relații externe)
- conflicte și crize (războaie, tensiuni, dezastre, urgențe)
- economie majoră (macroeconomic, politici fiscale, crize economice)
- fintech și inovații financiare (bănci, criptomonede, plăți digitale, reglementări)
- crimă și justiție (infracțiuni grave, anchete, decizii judecătorești importante)

Respinge:
- știri minore (evenimente locale fără impact național)
- opinii și editoriale
- divertisment, sport, lifestyle (dacă nu au legătură cu categoriile de mai sus)

Dacă NU este important: răspunde IGNORE

Dacă ESTE:
Creează un rezumat într-o singură propoziție în limba română.
Bazează-te STRICT pe informațiile din titlu și descriere. Nu adăuga detalii care nu apar acolo.
Folosește funcțiile și titlurile exacte ale persoanelor așa cum sunt menționate în text (de exemplu, "ministrul Finanțelor" dacă așa apare, nu "premierul").
Rezumatul poate începe cu un emoji relevant.

Răspunde DOAR cu un obiect JSON:
{{"ro": "rezumatul tău aici"}}
"""

    payload = {
        "model": "openrouter/auto",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 100
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
# 📡 RSS – extracts description
# ---------------------------------------------------------------------------

def fetch_rss_items(feed_url: str) -> List[Tuple[str, str, str]]:
    """
    Returns list of (link, title, description)
    """
    items = []
    try:
        req = urllib.request.Request(feed_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            root = ET.fromstring(response.read())

            for item in root.findall('.//item')[:MAX_ITEMS_PER_SOURCE]:
                title_elem = item.find('title')
                link_elem = item.find('link')
                desc_elem = item.find('description')

                if title_elem is None or link_elem is None:
                    continue

                title = title_elem.text.strip()
                link = link_elem.text.strip()
                description = desc_elem.text.strip() if desc_elem is not None else ""

                items.append((link, title, description))
    except Exception as e:
        logging.error(f"RSS error for {feed_url}: {e}")

    return items

# ---------------------------------------------------------------------------
# 📲 TELEGRAM – CLEAN, MINIMAL DESIGN
# ---------------------------------------------------------------------------

def post_to_telegram(source: str, summary: str, link: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    template = SOURCE_TEMPLATES.get(source, DEFAULT_TEMPLATE)
    message = template.format(summary=summary, link=link)

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
        logging.info(f"Posted successfully from {source}")

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

        for link, title, description in items:

            if is_repost(title):
                continue

            if link in seen_links:
                continue

            if is_same_event(title, seen_titles):
                continue

            summary = ask_ai_filter_and_summarize(title, description)
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
