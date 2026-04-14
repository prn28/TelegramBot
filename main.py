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
TOTAL_RUNTIME_SECONDS = int(5 * 3600)
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
    "TV8 Moldova":    "https://tv8.md/feed",
    "Ziarul de Gardă":"https://www.zdg.md/feed",
    "Newsmaker MD":   "https://newsmaker.md/feed",
    "Realitatea.md":  "https://realitatea.md/rss",
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
# XML & HISTORY HELPERS
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
        "Răspunde EXCLUSIV cu JSON valid, fără markdown, fără backtick-uri, fără alt text:\n"
        '{"results": [{"index": 1, "publish": true, "ro": "Guvernul a aprobat bugetul pentru 2025 cu un deficit de 5%.", "type": "politics"}]}\n'
        "Tipuri valide: politics, economy, conflict, other."
    )

    payload = {
        "model": "google/gemini-2.0-flash-001",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 800,
    }

    try:
        logging.info("  [AI] Trimit cerere la OpenRouter...")
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {OPEN_ROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=25) as res:
            response_body = res.read()

        data = json.loads(response_body)
        raw_text = data["choices"][0]["message"]["content"].strip()
        clean_text = re.sub(r"
http://googleusercontent.com/immersive_entry_chip/0
