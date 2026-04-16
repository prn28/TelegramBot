import urllib.request
import urllib.parse
import json
import xml.etree.ElementTree as ET
import time
import os
import re
import logging
import traceback
from datetime import datetime, timedelta
from typing import Set, Optional, Dict, List

# --- 🔐 CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPEN_ROUTER_API_KEY = os.getenv("OPEN_ROUTER_API_KEY")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

HISTORY_LINKS_FILE = "posted_links.txt"
HISTORY_TITLES_FILE = "posted_titles.txt"

# --- ⏱️ TIMING CONFIG ---
TOTAL_RUNTIME_SECONDS = int(4.5 * 3600)   # FIX #8: was 5h, reduced to 4.5h to avoid gap with 6h cron
CYCLE_INTERVAL_SECONDS = 3600
MOLDOVA_OFFSET = 3  # UTC+3 Chișinău

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
    "trump", "putin", "zelenski", "ursula", "ungaria", "orban",
    "petrol", "energie", "gaze", "blocadă", "sancțiuni", "embargo",
    "refugiați", "frontieră", "migrație",
]

BLACKLIST = ["horoscop", "vremea", "sport", "fotbal", "rețetă", "showbiz", "loto"]

SOURCES = {
    "TV8 Moldova":     "https://tv8.md/feed",
    "Ziarul de Gardă": "https://www.zdg.md/feed",
    "Newsmaker MD":    "https://newsmaker.md/feed",
    "Realitatea":      "https://realitatea.md/rss",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ---------------------------------------------------------------------------
# STARTUP CHECKS
# ---------------------------------------------------------------------------

def check_env():
    missing = [k for k, v in {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN,
        "OPEN_ROUTER_API_KEY": OPEN_ROUTER_API_KEY,
        "TELEGRAM_CHAT_ID": CHAT_ID,
    }.items() if not v]
    if missing:
        logging.error(f"LIPSESC VARIABILE DE MEDIU: {missing}")
        raise SystemExit(1)
    logging.info("✅ Variabile de mediu OK")

    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {OPEN_ROUTER_API_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            status = res.status
        logging.info(f"✅ OpenRouter accesibil (HTTP {status})")
    except Exception as e:
        logging.error(f"❌ OpenRouter ping EȘUAT: {e}")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# NETWORK HELPER — FIX #6: retry on transient errors
# ---------------------------------------------------------------------------

def fetch_url(url: str, headers: Optional[Dict] = None, retries: int = 2, timeout: int = 15) -> bytes:
    last_exc = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers=headers or {"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last_exc = e
            if attempt < retries:
                logging.warning(f"  Retry {attempt + 1}/{retries} pentru {url}: {e}")
                time.sleep(3)
    raise last_exc


# ---------------------------------------------------------------------------
# XML HELPERS
# ---------------------------------------------------------------------------

def sanitize_xml(raw: bytes) -> bytes:
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r'&(?!(?:#\d+|#x[\da-fA-F]+|amp|lt|gt|quot|apos);)', '&amp;', text)
    return text.encode("utf-8")


def parse_feed(raw: bytes):
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        return ET.fromstring(sanitize_xml(raw))


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
    t = title.lower()
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    stop = {"a", "al", "ale", "an", "că", "ce", "cu", "de", "din", "este",
            "eu", "fi", "i", "ia", "iar", "îi", "în", "înaltă", "înainte",
            "la", "mai", "o", "pe", "pentru", "prin", "sa", "se", "si", "și",
            "sau", "spre", "sub", "un", "una", "unui", "unii", "va"}
    words = [w for w in t.split() if w not in stop and len(w) > 2]
    return " ".join(words)


def titles_are_similar(new_title: str, seen_titles: Set[str], threshold: int = 4) -> bool:
    new_words = set(normalize_title(new_title).split())
    if len(new_words) < 3:
        return False
    for seen in seen_titles:
        seen_words = set(normalize_title(seen).split())
        if len(new_words & seen_words) >= threshold:
            return True
    return False


# ---------------------------------------------------------------------------
# GATE 1 – KEYWORD FILTER
# ---------------------------------------------------------------------------

def is_worth_ai_check(title: str) -> bool:
    t = title.lower()
    if any(w in t for w in BLACKLIST):
        return False
    return any(w in t for w in POLITICAL_KEYWORDS)


# ---------------------------------------------------------------------------
# GATE 2 – AI BATCH FILTER
# ---------------------------------------------------------------------------

def ask_ai_batch(candidates: List[Dict]) -> List[Optional[Dict]]:
    if not candidates:
        return []

    lines = []
    for i, c in enumerate(candidates):
        lines.append(f"{i+1}. Titlu: {c['title']}\n   Descriere: {c['description'][:120]}")
    numbered_list = "\n\n".join(lines)

    prompt = (
        "Ești editorul principal al canalului 'Republica News' — un canal de știri pentru cetățenii Republicii Moldova.\n\n"

        "PUBLICĂ dacă știrea se încadrează în oricare din aceste categorii:\n"
        "  A) Politică internă moldovenească — decizii de guvern, parlament, legi, alegeri, corupție, justiție\n"
        "  B) Economie care afectează Moldova — prețuri energie, gaz, electricitate, buget, FMI, BNM, inflație\n"
        "  C) Securitate și geopolitică regională — Ucraina, Transnistria, NATO, Rusia, tensiuni militare\n"
        "  D) Evenimente internaționale cu impact direct asupra Moldovei — sancțiuni, embargouri, relații UE,\n"
        "     decizii ale partenerilor strategici (SUA, Germania, România, Ungaria privind gaze/petrol, etc.)\n\n"

        "IGNORĂ doar dacă știrea nu are NICIUN impact rezonabil asupra Moldovei:\n"
        "  - Știri despre alte țări fără legătură cu Moldova sau regiunea\n"
        "  - Evenimente de rutină: inaugurări, vizite simbolice, declarații fără substanță\n"
        "  - Divertisment, sport, horoscop, meteo\n\n"

        "Când ești în dubiu — PUBLICĂ. Este mai bine să informezi decât să omiti.\n\n"

        "STILUL câmpului 'ro': Scrie ca un jurnalist — o propoziție directă, la subiect, la timpul prezent sau trecut.\n"
        "INTERZIS să începi cu: 'Articol despre', 'Știre despre', 'Un articol', 'Raport despre'.\n"
        "CORECT: 'Aliații NATO resping blocada portului impusă de Trump.'\n"
        "CORECT: 'Ungaria continuă importurile de gaze rusești, afectând securitatea energetică regională.'\n\n"

        f"{numbered_list}\n\n"

        "Răspunde EXCLUSIV cu JSON valid, fără markdown, fără backtick-uri, fără alt text:\n"
        '{"results": [{"index": 1, "publish": true, "ro": "Guvernul a aprobat bugetul pentru 2025 cu un deficit de 5%.", "type": "politics"}, '
        '{"index": 2, "publish": false}]}\n'
        "Tipuri valide: politics, economy, conflict, other."
    )

    payload = {
        "model": "google/gemini-2.0-flash-001",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 1500,   # FIX #2: was 600, raised to handle large batches
    }

    raw_text = ""
    try:
        logging.info("  [AI] Trimit cerere la OpenRouter...")
        # FIX #6: use fetch_url with retry for the AI call too
        response_body = fetch_url(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPEN_ROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            retries=2,
            timeout=30,
        )
        # fetch_url uses urlopen; we need to POST, so use manual request here
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {OPEN_ROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        last_exc = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as res:
                    http_status = res.status
                    response_body = res.read()
                break
            except Exception as e:
                last_exc = e
                if attempt < 2:
                    logging.warning(f"  [AI] Retry {attempt+1}/2: {e}")
                    time.sleep(3)
        else:
            raise last_exc

        logging.info(f"  [AI] HTTP status: {http_status}")
        data = json.loads(response_body)

        if "error" in data:
            logging.error(f"  [AI] API error în body: {data['error']}")
            return [None] * len(candidates)

        raw_text = data["choices"][0]["message"]["content"].strip()
        logging.info(f"  [AI raw]: {raw_text[:800]}")

        # FIX #5: robust JSON extraction — find the outermost {...} block
        match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        clean_text = match.group(0) if match else raw_text

        parsed = json.loads(clean_text)
        result_map = {r["index"]: r for r in parsed.get("results", [])}

        output = []
        for i, _ in enumerate(candidates):
            r = result_map.get(i + 1)
            if r and r.get("publish") and "ro" in r:
                output.append({"ro": r["ro"], "type": r.get("type", "other")})
            else:
                output.append(None)

        approved = sum(1 for x in output if x)
        logging.info(f"  [AI] {approved}/{len(candidates)} aprobate")
        return output

    except json.JSONDecodeError as e:
        logging.error(f"  [AI] JSON parse error: {e}")
        logging.error(f"  [AI] Text care a eșuat: {raw_text[:400]}")
        return [None] * len(candidates)
    except Exception as e:
        logging.error(f"  [AI] Eroare neașteptată: {type(e).__name__}: {e}")
        logging.error(traceback.format_exc())
        return [None] * len(candidates)


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

def post_to_telegram(source: str, summary: str, n_type: str, link: str) -> None:
    badges = {
        "politics": "🏛️ POLITICĂ",
        "economy":  "🏦 ECONOMIE",
        "conflict": "🛡️ SECURITATE",
        "breaking": "⚠️ BREAKING",
    }
    badge = badges.get(n_type, "📰 ȘTIRI")
    # FIX #1: was broken f-string with invalid | pipe operator
    message = (
        f"🌟 <b>Republica News</b> | {badge}\n"
        f"\n"
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
        logging.info("  📨 Telegram OK")
    except Exception as e:
        logging.error(f"  Telegram error: {e}")


# ---------------------------------------------------------------------------
# MAIN CYCLE
# ---------------------------------------------------------------------------

def run_cycle(cycle_num: int) -> None:
    seen_links  = load_set(HISTORY_LINKS_FILE)
    seen_titles = load_set(HISTORY_TITLES_FILE)

    logging.info(f"--- Republica News: Ciclul {cycle_num} — colectare candidați ---")

    candidates: List[Dict] = []

    for source, rss_url in SOURCES.items():
        try:
            raw = fetch_url(rss_url)   # FIX #6: uses retry helper
            root = parse_feed(raw)

            for item in root.findall(".//item")[:3]:
                link_el  = item.find("link")
                title_el = item.find("title")
                desc_el  = item.find("description")

                if title_el is None:
                    continue

                title = (title_el.text or "").strip()

                # FIX #7: fallback to <guid> when <link> is missing or empty
                link = (link_el.text or "").strip() if link_el is not None else ""
                if not link:
                    guid_el = item.find("guid")
                    if guid_el is not None and guid_el.attrib.get("isPermaLink") != "false":
                        link = (guid_el.text or "").strip()

                desc = (desc_el.text or "").strip() if desc_el is not None else ""

                if not link or not title:
                    continue

                if link in seen_links:
                    continue

                if titles_are_similar(title, seen_titles):
                    logging.info(f"  Titlu similar ignorat: {title[:60]}")
                    continue

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
    for i, c in enumerate(candidates):
        logging.info(f"    {i+1}. [{c['source']}] {c['title'][:70]}")

    results = ask_ai_batch(candidates)

    posted = 0
    for candidate, result in zip(candidates, results):
        if result:
            # FIX #4: re-check similarity post-AI to catch duplicates from different sources
            if titles_are_similar(candidate["title"], seen_titles):
                logging.info(f"  Titlu duplicat post-AI, ignorat: {candidate['title'][:60]}")
                continue

            post_to_telegram(candidate["source"], result["ro"], result["type"], candidate["link"])
            save_item(HISTORY_LINKS_FILE,  candidate["link"])
            save_item(HISTORY_TITLES_FILE, candidate["title"])
            seen_links.add(candidate["link"])
            seen_titles.add(candidate["title"])
            logging.info(f"  ✅ Postat: {candidate['title'][:70]}")
            posted += 1
            time.sleep(2)

    logging.info(f"  Ciclul {cycle_num} complet: {posted} știri postate din {len(candidates)} candidați.")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    check_env()

    run_start = time.time()
    cycle_num = 0

    while True:
        elapsed = time.time() - run_start
        if elapsed >= TOTAL_RUNTIME_SECONDS:
            logging.info("Timpul limită atins. Închidere.")
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
