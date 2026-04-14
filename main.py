import urllib.request
import urllib.parse
import json
import xml.etree.ElementTree as ET
import time
import os
import re
import logging
from datetime import datetime, timedelta
from typing import Set, Optional, Dict, List

# --- 🔐 CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPEN_ROUTER_API_KEY = os.getenv("OPEN_ROUTER_API_KEY")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

HISTORY_LINKS_FILE = "posted_links.txt"
HISTORY_TITLES_FILE = "posted_titles.txt"

# --- ⏱️ TIMING CONFIG ---
TOTAL_RUNTIME_SECONDS = int(5 * 3600)   # 5 hours total
CYCLE_INTERVAL_SECONDS = 3600            # Collect + post once per hour
MOLDOVA_OFFSET = 3                       # UTC+3 Chișinău

# --- 🌍 KEYWORD FILTER (Gate 1) ---
POLITICAL_KEYWORDS = [
    "guvern", "parlament", "președinție", "minister", "deputat",
    "vot", "lege", "proiect de lege", "hotărâre", "decret",
    "curtea constituțională", "cec", "cna", "sis",
    "sandu", "recean", "grosu", "ceban", "spînu", "alaiba",
    "nosatîi", "popșoi", "dodon", "voronin", "chicu", "usatîi", "șor",
    "ue", "uniunea europeană", "nato", "aderare", "integrare",
    "kremlin", "moscova", "bucurești", "kiev", "washington",
    "transnistria", "ucraina", "război", "securitate", "diplomație",
    "economie", "pib", "buget", "gaz", "moldovagaz", "energocom",
    "tarif", "electricitate", "inflație", "bnm", "fmi", "banca mondială",
    "justiție", "procuror", "judecător", "corupție", "arest", "percheziții",
    "protest", "manifestație", "atac", "tensiuni",
]

BLACKLIST = ["horoscop", "vremea", "sport", "fotbal", "rețetă", "showbiz", "loto"]

SOURCES = {
    "TV8 Moldova":    "https://tv8.md/feed",
    "Ziarul de Gardă":"https://www.zdg.md/feed",
    "Newsmaker MD":   "https://newsmaker.md/feed",
    "Realitatea.md":  "https://realitatea.md/rss",
    "MOLDPRES":       "https://moldpres.md/config/rss.php?lang=rom",
    "Agora.md":       "https://agora.md/rss",
    "Jurnal.md":      "https://jurnal.md/rss",
    "Știri.md":       "https://stiri.md/feed",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ---------------------------------------------------------------------------
# HISTORY HELPERS
# ---------------------------------------------------------------------------

def load_set(filepath: str) -> Set[str]:
    if not os.path.exists(filepath):
        return set()
    with open(filepath, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_item(filepath: str, value: str) -> None:
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(value + "\n")


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and filler words for fuzzy dedup."""
    t = title.lower()
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    # Remove very common Romanian filler words so titles are compared by substance
    stop = {"a", "al", "ale", "an", "că", "ce", "cu", "de", "din", "este",
            "eu", "fi", "i", "ia", "iar", "îi", "în", "înaltă", "înainte",
            "la", "mai", "o", "pe", "pentru", "prin", "sa", "se", "si", "și",
            "sau", "spre", "sub", "un", "una", "unui", "unii", "va"}
    words = [w for w in t.split() if w not in stop and len(w) > 2]
    return " ".join(words)


def titles_are_similar(new_title: str, seen_titles: Set[str], threshold: int = 4) -> bool:
    """
    Returns True if `new_title` shares >= `threshold` meaningful words
    with any already-seen title — catches the same story from multiple sources.
    """
    new_words = set(normalize_title(new_title).split())
    if len(new_words) < 3:
        return False
    for seen in seen_titles:
        seen_words = set(normalize_title(seen).split())
        overlap = len(new_words & seen_words)
        if overlap >= threshold:
            return True
    return False


# ---------------------------------------------------------------------------
# GATE 1 – KEYWORD FILTER (cheap, local)
# ---------------------------------------------------------------------------

def is_worth_ai_check(title: str) -> bool:
    t = title.lower()
    if any(w in t for w in BLACKLIST):
        return False
    return any(w in t for w in POLITICAL_KEYWORDS)


# ---------------------------------------------------------------------------
# GATE 2 – AI FILTER (one call per candidate, batch prompt to save tokens)
# ---------------------------------------------------------------------------

def ask_ai_batch(candidates: List[Dict]) -> List[Optional[Dict]]:
    """
    Send up to ~10 candidates in ONE API call.
    Returns a list of results aligned with `candidates`;
    None means the item should be ignored.
    """
    if not candidates:
        return []

    lines = []
    for i, c in enumerate(candidates):
        lines.append(f"{i+1}. Titlu: {c['title']}\n   Descriere: {c['description'][:120]}")
    numbered_list = "\n\n".join(lines)

    prompt = f"""Ești editorul principal al canalului 'Republica News' din Moldova.
Analizează lista de știri de mai jos și decide care merită publicate.

CRITERII:
- Publică DOAR știri politice sau economice cu impact NAȚIONAL major.
- Ignoră: rutine administrative, vizite de curtoazie, declarații fără substanță.
- Acceptă: schimbări de legi, crize, decizii economice mari, securitate națională.

{numbered_list}

Răspunde EXCLUSIV cu un obiect JSON valid, fără altceva, fără markdown:
{{
  "results": [
    {{"index": 1, "publish": true,  "ro": "rezumat scurt o propoziție", "type": "politics"}},
    {{"index": 2, "publish": false}},
    ...
  ]
}}
Tipuri valide: politics, economy, conflict, other.
"""

    payload = {
        "model": "google/gemini-2.0-flash-001",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 600,
    }

    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {OPEN_ROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as res:
            data = json.loads(res.read())
            text = data["choices"][0]["message"]["content"].strip()
            # Strip markdown fences if present
            text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
            parsed = json.loads(text)
            result_map = {r["index"]: r for r in parsed.get("results", [])}
            output = []
            for i, _ in enumerate(candidates):
                r = result_map.get(i + 1)
                if r and r.get("publish") and "ro" in r:
                    output.append({"ro": r["ro"], "type": r.get("type", "other")})
                else:
                    output.append(None)
            return output
    except Exception as e:
        logging.error(f"AI batch error: {e}")
        return [None] * len(candidates)


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

def post_to_telegram(source: str, summary: str, n_type: str, link: str) -> None:
    badges = {
        "politics": "🏛️ POLITIC",
        "economy":  "🏦 ECONOMIE",
        "conflict": "🛡️ SECURITATE",
        "breaking": "⚠️ BREAKING",
    }
    badge = badges.get(n_type, "📰 ȘTIRI")
    message = (
        f"🌟 <b>Republica News</b>\n"
        f"{badge} | {source}\n"
        f"—\n"
        f"<i>{summary}</i>\n\n"
        f"🔗 <a href='{link}'>Citește articolul complet</a>"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req)
    except Exception as e:
        logging.error(f"Telegram error: {e}")


# ---------------------------------------------------------------------------
# MAIN CYCLE
# ---------------------------------------------------------------------------

def run_cycle(cycle_num: int) -> None:
    seen_links  = load_set(HISTORY_LINKS_FILE)
    seen_titles = load_set(HISTORY_TITLES_FILE)

    logging.info(f"--- Republica News: Ciclul {cycle_num} — colectare candidați ---")

    candidates: List[Dict] = []   # {source, title, description, link}

    # --- Step 1: Collect candidates (keyword-filtered, URL-deduped, title-deduped) ---
    for source, rss_url in SOURCES.items():
        try:
            req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as response:
                root = ET.fromstring(response.read())
                for item in root.findall(".//item")[:3]:   # top 3 per source
                    link_el  = item.find("link")
                    title_el = item.find("title")
                    desc_el  = item.find("description")

                    if link_el is None or title_el is None:
                        continue

                    link  = link_el.text.strip()
                    title = title_el.text.strip()
                    desc  = desc_el.text.strip() if desc_el is not None and desc_el.text else ""

                    # Gate 0: URL already posted?
                    if link in seen_links:
                        continue

                    # Gate 0b: Similar title already seen this session or ever?
                    if titles_are_similar(title, seen_titles):
                        logging.info(f"  Titlu similar ignorat: {title[:50]}")
                        continue

                    # Gate 1: keyword filter
                    if not is_worth_ai_check(title):
                        continue

                    candidates.append({"source": source, "title": title,
                                       "description": desc, "link": link})
        except Exception as e:
            logging.error(f"Eroare sursă {source}: {e}")

    if not candidates:
        logging.info("  Niciun candidat nou găsit.")
        return

    logging.info(f"  {len(candidates)} candidat(i) trimit la AI (1 singur apel)")

    # --- Step 2: ONE batch AI call for all candidates ---
    results = ask_ai_batch(candidates)

    # --- Step 3: Post approved items ---
    posted = 0
    for candidate, result in zip(candidates, results):
        if result:
            post_to_telegram(candidate["source"], result["ro"], result["type"], candidate["link"])
            save_item(HISTORY_LINKS_FILE,  candidate["link"])
            save_item(HISTORY_TITLES_FILE, candidate["title"])
            seen_links.add(candidate["link"])
            seen_titles.add(candidate["title"])
            logging.info(f"  ✅ Postat: {candidate['title'][:60]}")
            posted += 1
            time.sleep(2)   # brief pause between Telegram messages

    logging.info(f"  Ciclul {cycle_num} complet: {posted} știri postate din {len(candidates)} candidați.")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_start   = time.time()
    cycle_num   = 0

    while True:
        elapsed = time.time() - run_start
        if elapsed >= TOTAL_RUNTIME_SECONDS:
            logging.info("Timpul limită atins (5h). Închidere.")
            break

        cycle_num  += 1
        cycle_start = time.time()
        run_cycle(cycle_num)

        remaining = TOTAL_RUNTIME_SECONDS - (time.time() - run_start)
        if remaining <= 0:
            break

        cycle_duration = time.time() - cycle_start
        sleep_time = max(0, min(CYCLE_INTERVAL_SECONDS - cycle_duration, remaining))

        chisinau_now = datetime.utcnow() + timedelta(hours=MOLDOVA_OFFSET)
        logging.info(
            f"Ora Chișinău: {chisinau_now.strftime('%H:%M')}. "
            f"Următorul scan în {sleep_time / 60:.1f} min."
        )
        time.sleep(sleep_time)
