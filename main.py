import urllib.request
import urllib.parse
import json
import xml.etree.ElementTree as ET
import time
import os
import re
import logging
from datetime import datetime, timedelta
from typing import Set, Optional, List, Dict

# --- 🔐 CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPEN_ROUTER_API_KEY = os.getenv("OPEN_ROUTER_API_KEY")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
HISTORY_FILE = "posted_links.txt"

# --- ⏱️ TIMING CONFIG ---
TOTAL_RUNTIME_SECONDS = int(5.5 * 3600) 
MOLDOVA_OFFSET = 3  # UTC+3 for Chisinau Summer Time

# --- 🌍 COMPREHENSIVE MOLDAVIAN KEYWORDS (Gate 1) ---
POLITICAL_KEYWORDS = [
    "guvern", "parlament", "președinție", "minister", "deputat", "cancelarie", 
    "consiliu", "primărie", "vot", "lege", "proiect de lege", "hotărâre", 
    "decret", "sesizare", "curtea constituțională", "cec", "cna", "sis",
    "sandu", "recean", "grosu", "ceban", "spînu", "alaiba", "nosatîi", 
    "popșoi", "vlah", "dodon", "voronin", "chicu", "usatîi", "șor", "tauber",
    "ue", "uniunea europeană", "nato", "aderare", "integrare", "bruxelles", 
    "kremlin", "moscova", "bucurești", "kiev", "washington", "summit", 
    "ambasador", "diplomație", "negocieri", "război", "ucraina", "transnistria",
    "economie", "pib", "buget", "anre", "gaz", "moldovagaz", "energocom", 
    "tarif", "electricitate", "preț", "inflație", "bancă", "bnm", "fmi", 
    "banca mondială", "credit", "împrumut", "investiții", "afaceri", "fintech",
    "justiție", "procuror", "judecător", "anchetă", "dosar", "corupție", 
    "sentință", "arest", "percheziții", "securitate", "poliție", "armată", 
    "frontiere", "atac", "tensiuni", "protest", "manifestație"
]

BLACKLIST = ["horoscop", "vremea", "sport", "fotbal", "rețetă", "showbiz", "loto"]

SOURCES = {
    "TV8 Moldova": "https://tv8.md/feed",
    "Ziarul de Gardă": "https://www.zdg.md/feed",
    "Newsmaker MD": "https://newsmaker.md/feed",
    "Realitatea.md": "https://realitatea.md/rss",
    "MOLDPRES": "https://moldpres.md/config/rss.php?lang=rom",
    "Agora.md": "https://agora.md/rss",
    "Jurnal.md": "https://jurnal.md/rss",
    "Știri.md": "https://stiri.md/feed",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# --- 🛠️ GATE 1: LOCAL KEYWORD FILTER ---
def is_worth_checking_with_ai(title: str) -> bool:
    t = title.lower()
    if any(word in t for word in BLACKLIST): return False
    return any(word in t for word in POLITICAL_KEYWORDS)

# --- 🧠 GATE 2: AI EDITORIAL FILTER ---
def ask_ai_analysis(title: str, description: str) -> Optional[Dict[str, str]]:
    url = "https://openrouter.ai/api/v1/chat/completions"
    
    # Stricter prompt to ensure only CRUCIAL news is posted
    prompt = f"""
    Ești editorul principal pentru canalul 'Republica News' din Moldova. 
    Misiunea ta: Postează DOAR știri politice sau economice cu impact NAȚIONAL major.
    
    Știre: {title}
    Descriere: {description[:150]}
    
    CRITERII DE FILTRARE:
    - Ignoră evenimente de rutină, vizite de curtoazie sau declarații fără substanță.
    - Acceptă doar schimbări de legi, crize, decizii economice mari sau securitate națională.
    - Dacă nu este crucial, răspunde exact cu: "IGNORE"
    
    Dacă este crucial, răspunde DOAR cu JSON:
    {{"ro": "rezumat scurt și profesional de o singură propoziție", "type": "politics/economy/conflict/other"}}
    """

    payload = {
        "model": "google/gemini-2.0-flash-001", 
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 120
    }

    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {OPEN_ROUTER_API_KEY}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as res:
            data = json.loads(res.read())
            text = data["choices"][0]["message"]["content"].strip()
            if "IGNORE" in text.upper(): return None
            match = re.search(r'\{.*\}', text, re.DOTALL)
            return json.loads(match.group()) if match else None
    except:
        return None

def post_to_telegram(source, summary, n_type, link):
    badges = {"politics": "🏛️ POLITIC", "economy": "🏦 ECONOMIE", "conflict": "🛡️ SECURITATE", "breaking": "⚠️ BREAKING"}
    badge = badges.get(n_type, "📰 ȘTIRI")
    
    # Updated Header to Republica News
    message = f"🌟 <b>Republica News</b>\n{badge} | {source}\n—\n<i>{summary}</i>\n\n🔗 <a href='{link}'>Citește articolul complet</a>"
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req)
    except Exception as e:
        logging.error(f"Telegram Error: {e}")

def run_cycle(cycle_num):
    seen_links = set(open(HISTORY_FILE).read().splitlines()) if os.path.exists(HISTORY_FILE) else set()
    logging.info(f"--- Republica News: Ciclul {cycle_num} ---")

    for source, rss_url in SOURCES.items():
        try:
            req = urllib.request.Request(rss_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as response:
                root = ET.fromstring(response.read())
                for item in root.findall('.//item')[:2]:
                    link = item.find('link').text.strip()
                    title = item.find('title').text.strip()
                    desc = item.find('description').text.strip() if item.find('description') is not None else ""

                    if link in seen_links: continue
                    if not is_worth_checking_with_ai(title): continue

                    result = ask_ai_analysis(title, desc)
                    if result and "ro" in result:
                        post_to_telegram(source, result["ro"], result["type"], link)
                        with open(HISTORY_FILE, "a") as f: f.write(link + "\n")
                        seen_links.add(link)
                        logging.info(f"Postat: {title[:30]}...")
                    
                    time.sleep(2) 
        except Exception as e:
            logging.error(f"Eroare sursă {source}: {e}")

if __name__ == "__main__":
    run_start = time.time()
    cycle_num = 0

    while True:
        elapsed = time.time() - run_start
        if elapsed >= TOTAL_RUNTIME_SECONDS:
            logging.info("Timpul limită atins (5.5h). Închidere.")
            break

        cycle_num += 1
        cycle_start = time.time()
        run_cycle(cycle_num)

        chisinau_now = datetime.utcnow() + timedelta(hours=MOLDOVA_OFFSET)
        current_hour = chisinau_now.hour
        
        # Day (07-23): 30 min | Night: 60 min
        interval = (30 * 60) if (7 <= current_hour < 23) else (60 * 60)

        cycle_duration = time.time() - cycle_start
        remaining_total = TOTAL_RUNTIME_SECONDS - (time.time() - run_start)

        if remaining_total <= 0: break
        sleep_time = max(0, min(interval - cycle_duration, remaining_total))
        logging.info(f"Ora Chișinău: {current_hour}. Următorul scan în {sleep_time/60:.1f} min.")
        time.sleep(sleep_time)
