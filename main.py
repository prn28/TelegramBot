import urllib.request
import json
import xml.etree.ElementTree as ET
import time
import os
import re
import logging
from typing import Set, Optional, List, Tuple, Dict, Any

# --- CONFIG ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPEN_ROUTER_API_KEY = os.getenv("OPEN_ROUTER_API_KEY")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

HISTORY_FILE = "posted_links.txt"
TITLE_HISTORY_FILE = "posted_titles.txt"

REQUEST_TIMEOUT = 15
RATE_LIMIT_SLEEP = 2
MAX_ITEMS_PER_SOURCE = 3  # Reduced from 5 to lower volume

logging.basicConfig(level=logging.INFO)

# ========== SOURCES ==========
SOURCES = {
    "TV8 Moldova": "https://tv8.md/feed",
    "Ziarul de Gardă": "https://www.zdg.md/feed",
    "Newsmaker MD": "https://newsmaker.md/feed",
    "Realitatea.md": "https://realitatea.md/rss",
    "MOLDPRES": "https://moldpres.md/config/rss.php?lang=rom",
    "MOLDPRES Sinteza": "https://moldpres.md/config/rssSinteza.php?lang=rom",
    "Stiri.md": "https://stiri.md/rss",
    "Canal Regional TV": "https://canalregionaltv.md/feed",
    "Media TV": "https://mediatv.md/feed",
    "MOVCA": "https://movca.md/feed",
    "ASE MD": "https://ase.md/feed",
    "Biofood": "https://biofood.md/feed",
}

# ========== PER‑SOURCE TEMPLATES (clean, professional) ==========
SOURCE_TEMPLATES = {
    "TV8 Moldova": (
        "📺 <b>TV8 Moldova</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Citește articolul</a>"
    ),
    "Ziarul de Gardă": (
        "📰 <b>Ziarul de Gardă</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Citește articolul</a>"
    ),
    "Newsmaker MD": (
        "📢 <b>Newsmaker MD</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Citește articolul</a>"
    ),
    "Realitatea.md": (
        "📡 <b>Realitatea.md</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Citește articolul</a>"
    ),
    "MOLDPRES": (
        "🏛️ <b>MOLDPRES</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Citește articolul</a>"
    ),
    "MOLDPRES Sinteza": (
        "🏛️ <b>MOLDPRES Sinteza</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Citește articolul</a>"
    ),
    "Stiri.md": (
        "📌 <b>Stiri.md</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Citește articolul</a>"
    ),
    "Canal Regional TV": (
        "📺 <b>Canal Regional TV</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Citește articolul</a>"
    ),
    "Media TV": (
        "📡 <b>Media TV</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Citește articolul</a>"
    ),
    "MOVCA": (
        "🎭 <b>MOVCA</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Citește articolul</a>"
    ),
    "ASE MD": (
        "📊 <b>ASE Moldova</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Citește articolul</a>"
    ),
    "Biofood": (
        "🌱 <b>Biofood</b>\n\n"
        "{summary}\n\n"
        "🔗 <a href='{link}'>Citește articolul</a>"
    ),
}
DEFAULT_TEMPLATE = "{summary}\n\n🔗 <a href='{link}'>Citește articolul</a>"

# Map news types to Romanian badges
TYPE_BADGES = {
    "breaking": "🔴 BREAKING",
    "politics": "🏛️ POLITIC",
    "economy": "💰 ECONOMIE",
    "crime": "⚖️ JUSTIȚIE",
    "conflict": "⚔️ CONFLICT",
    "fintech": "💳 FINTECH",
    "analysis": "📊 ANALIZĂ",
    "opinion": "💬 OPINIE",
    "local": "📍 LOCAL",
    "international": "🌍 INTERNAȚIONAL",
    "other": "📰 ȘTIRI",
}
DEFAULT_BADGE = "📰 ȘTIRI"

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
# 🤖 AI: FILTER + SUMMARY + TYPE (STRICTER PROMPT)
# ---------------------------------------------------------------------------

def ask_ai_filter_and_summarize(title: str, description: str = "") -> Optional[Dict[str, str]]:
    """
    Returns dict with 'ro' (summary) and 'type' (category) or None if ignored/error.
    """
    url = "https://openrouter.ai/api/v1/chat/completions"

    content = f"Titlu: {title}\n"
    if description:
        content += f"Descriere: {description}\n"

    prompt = f"""
Ești editor pentru un canal de știri foarte selectiv, care publică DOAR știri cu impact major național sau internațional.

{content}

📌 **Categorii acceptate** (doar dacă au impact semnificativ):
- Politică majoră: decizii guvernamentale, legi importante, alegeri, crize politice, relații externe
- Economie majoră: buget, taxe, crize economice, acorduri financiare internaționale
- Conflicte și securitate: războaie, atacuri, tensiuni grave, dezastre
- Justiție de mare interes: anchete de corupție la nivel înalt, sentințe importante
- Fintech/bani: reglementări financiare majore, inovații cu impact larg

🚫 **Respinge categoric** (indiferent de sursă):
- Evenimente locale fără ecou național (ex: inaugurări de magazine, accidente minore, evenimente culturale locale)
- Știri din sport, divertisment, lifestyle, vreme, animale, oameni, vedete
- Anunțuri de rutină (ex: întreruperi apă/curent, concursuri, burse)
- Opinii, editoriale, interviuri fără valoare de știre
- Știri care sunt în esență reclame sau promovări
- Subiecte care nu afectează direct Moldova sau nu au relevanță pentru publicul moldovean

❗ **Reguli stricte**:
- Dacă știrea nu este clar în categoriile acceptate și de impact major → răspunde IGNORE
- Nu inventa detalii. Folosește strict informațiile din titlu și descriere.
- Păstrează titlurile oficiale ale persoanelor (ex: "ministrul Finanțelor" nu "premierul").
- Rezumatul: o singură propoziție, în română, clară și concisă. Poate începe cu un emoji relevant.
- Clasifică știrea în unul dintre tipurile: breaking, politics, economy, crime, conflict, fintech, analysis, opinion, local, international, other.

Răspunde DOAR cu un obiect JSON:
{{"ro": "rezumat", "type": "tip"}}
"""

    payload = {
        "model": "openrouter/auto",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 120
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

            if "ignore" in text.lower():
                return None

            # Try to extract JSON from response (in case AI adds extra text)
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                json_str = match.group()
            else:
                json_str = text

            try:
                parsed = json.loads(json_str)
                if "ro" in parsed:
                    return {
                        "summary": parsed["ro"],
                        "type": parsed.get("type", "other").lower()
                    }
                else:
                    logging.error(f"AI response missing 'ro' field: {parsed}")
                    return None
            except json.JSONDecodeError:
                logging.error(f"AI response not valid JSON: {text}")
                return None

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

def fetch_rss_items(feed_url: str) -> List[Tuple[str, str, str]]:
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

                title = title_elem.text.strip() if title_elem.text else ""
                link = link_elem.text.strip() if link_elem.text else ""
                description = desc_elem.text.strip() if desc_elem is not None and desc_elem.text else ""

                if title and link:
                    items.append((link, title, description))
    except Exception as e:
        logging.error(f"RSS error for {feed_url}: {e}")

    return items

# ---------------------------------------------------------------------------
# 📲 TELEGRAM
# ---------------------------------------------------------------------------

def post_to_telegram(source: str, summary_with_badge: str, link: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    template = SOURCE_TEMPLATES.get(source, DEFAULT_TEMPLATE)
    message = template.format(summary=summary_with_badge, link=link)

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
        if not items:
            logging.info(f"No items from {source}, skipping.")
            continue

        for link, title, description in items:

            if is_repost(title):
                continue

            if link in seen_links:
                continue

            if is_same_event(title, seen_titles):
                continue

            result = ask_ai_filter_and_summarize(title, description)
            if not result:
                continue

            summary = result["summary"]
            news_type = result["type"]

            # Get badge for this type, fallback to default
            badge = TYPE_BADGES.get(news_type, DEFAULT_BADGE)
            summary_with_badge = f"{badge}\n{summary}"

            post_to_telegram(source, summary_with_badge, link)

            save_to_history(link)
            save_title_history(title)

            seen_links.add(link)
            seen_titles.append(normalize_title(title))

            time.sleep(RATE_LIMIT_SLEEP)

        time.sleep(RATE_LIMIT_SLEEP)

if __name__ == "__main__":
    run()
