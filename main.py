import urllib.request
import json
import xml.etree.ElementTree as ET
import time
import os
import re
import logging
from typing import Set, Optional, List, Tuple, Dict, Any

#CONFIGURATION
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPEN_ROUTER_API_KEY = os.getenv("OPEN_ROUTER_API_KEY")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

HISTORY_FILE = "posted_links.txt"
TITLE_HISTORY_FILE = "posted_titles.txt"

REQUEST_TIMEOUT = 15
RATE_LIMIT_SLEEP = 2
MAX_ITEMS_PER_SOURCE = 3

# --- LOOP CONFIG ---
CYCLE_INTERVAL_SECONDS = 30 * 60   # 30 minutes between cycles
TOTAL_RUNTIME_SECONDS  = int(5.5 * 3600)  # 5 hours 30 minutes = 19800 s

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ========== SOURCES ==========
SOURCES = {
    "TV8 Moldova": "https://tv8.md/feed",
    "Ziarul de Gardă": "https://www.zdg.md/feed",
    "Newsmaker MD": "https://newsmaker.md/feed",
    "Realitatea.md": "https://realitatea.md/rss",
    "MOLDPRES": "https://moldpres.md/config/rss.php?lang=rom",
    "MOLDPRES Sinteza": "https://moldpres.md/config/rssSinteza.php?lang=rom",
    "Media TV": "https://mediatv.md/feed",
    "MOVCA": "https://movca.md/feed",
    "ASE MD": "https://ase.md/feed",
    "IPN": "https://www.ipn.md/rss",
    "Agora.md": "https://agora.md/rss",
    "Jurnal.md": "https://jurnal.md/rss",
    "Timpul.md": "https://timpul.md/feed",
    "Știri.md": "https://stiri.md/feed",
}

#PER‑SOURCE TEMPLATES
SOURCE_TEMPLATES = {
    source: (
        f"{get_emoji(source)} <b>{source}</b>\n"
        "—\n"
        "<i>{{summary}}</i>\n\n"
        "🔗 <a href='{{link}}'>Citește articolul</a>"
    ) for source in SOURCES
}
DEFAULT_TEMPLATE = "📌 <b>Știri</b>\n—\n<i>{{summary}}</i>\n\n🔗 <a href='{{link}}'>Citește articolul</a>"

def get_emoji(source: str) -> str:
    emojis = {
        "TV8 Moldova": "📺",
        "Ziarul de Gardă": "📰",
        "Newsmaker MD": "📢",
        "Realitatea.md": "📡",
        "MOLDPRES": "🏛️",
        "MOLDPRES Sinteza": "🏛️",
        "Media TV": "📡",
        "MOVCA": "🎭",
        "ASE MD": "📊",
        "IPN": "📰",
        "Agora.md": "📌",
        "Jurnal.md": "📰",
        "Timpul.md": "⏰",
        "Știri.md": "🗞️",
    }
    return emojis.get(source, "📌")

TYPE_BADGES = {
    "alert": "🚨 ALERTĂ 🚨",
    "breaking": "⚠️ 🔴 BREAKING NEWS",
    "politics": "🏛️ POLITIC",
    "economy": "🏦 ECONOMIE",
    "crime": "⚖️ JUSTIȚIE",
    "conflict": "🛡️ SECURITATE",
    "fintech": "💸 FINTECH",
    "analysis": "📊🧠 ANALIZĂ",
    "opinion": "💬 OPINIE",
    "local": "📍 LOCAL",
    "international": "🌐 INTERNAȚIONAL",
    "other": "📰 ȘTIRI",
}
DEFAULT_BADGE = "📰 ȘTIRI"

# 🧠 TITLE HELPERS

def normalize_title(title: str) -> str:
    title = title.lower()
    title = re.sub(r'sursa[:\-].*', '', title)
    title = re.sub(r'[^\w\s]', '', title)
    return re.sub(r'\s+', ' ', title).strip()

def is_repost(title: str) -> bool:
    t = title.lower()
    return "sursa:" in t or "source:" in t or "preluat" in t

# 📁 HISTORY

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

# 🤖 AI: FILTER + SUMMARY + TYPE 

def extract_json(text: str) -> Optional[dict]:
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        return None
    json_str = match.group()
    json_str = re.sub(r'"""', '"', json_str)
    if json_str.count('{') > json_str.count('}'):
        json_str += '}'
    if json_str.count('"') % 2 != 0:
        json_str += '"'
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        ro_match = re.search(r'"ro"\s*:\s*"([^"]*)"', json_str)
        type_match = re.search(r'"type"\s*:\s*"([^"]*)"', json_str)
        if ro_match:
            result = {"ro": ro_match.group(1)}
            if type_match:
                result["type"] = type_match.group(1)
            return result
        return None

def ask_ai_filter_and_summarize(title: str, description: str = "") -> Optional[Dict[str, str]]:
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
- Politică națională: decizii guvernamentale, legi, alegeri, crize politice, relații externe (inclusiv integrarea europeană, relația cu România, Ucraina, Rusia)
- Economie și afaceri: buget, taxe, investiții, mediul de afaceri, acorduri financiare internaționale (FMI, UE), energie, agricultură, infrastructură
- Fintech și inovații financiare: bănci, criptomonede, plăți digitale, reglementări financiare, startup-uri
- Justiție de mare interes: anchete de corupție la nivel înalt, sentințe importante, cazuri de crimă organizată
- Securitate și conflicte: război din Ucraina, impact asupra Moldovei, tensiuni în regiune, crize de securitate
- Evenimente internaționale care afectează direct Moldova sau au o relevanță clară pentru cetățenii moldoveni (ex: sancțiuni, acorduri UE, evoluții în țările vecine)
- Include știri despre vedete dacă sunt cumva relevante la viața politico, socio-economică a Republicii Moldova
- Include opinii de la politiceni, editoriale, și comentarii din interviuri (doar dacă sunt pe un subiect important din punct de vedere socio-economic)

🚫 **Respinge categoric** (indiferent de sursă):
- Evenimente locale fără ecou național (ex: inaugurări de magazine, accidente minore, evenimente culturale locale)
- Știri din sport, divertisment, lifestyle, vreme, animale, oameni, vedete
- Anunțuri de rutină (ex: întreruperi apă/curent, concursuri, burse)
- Opinii, editoriale, interviuri fără valoare de știre
- Știri care sunt în esență reclame sau promovări
- Subiecte care nu afectează direct Moldova sau nu au relevanță pentru publicul moldovean

🚨 **Două categorii speciale** (folosește-le după cum urmează):
- **alert** – pentru evenimente grave: catastrofe naturale, accidente majore cu victime multiple, dezastre, stări de urgență națională, calamități (indiferent dacă se întâmplă în Moldova sau în lume, dar cu impact relevant)
- **breaking** – pentru crize de securitate, escaladări militare grave, cazuri de corupție la cel mai înalt nivel, demisii bruște ale unor oficiali majori, evenimente care schimbă fundamental situația politică sau de securitate

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

            parsed = extract_json(text)
            if parsed and "ro" in parsed:
                return {
                    "summary": parsed["ro"],
                    "type": parsed.get("type", "other").lower()
                }
            else:
                logging.error(f"Could not extract valid JSON from AI response: {text}")
                return None

    except Exception as e:
        logging.error(f"AI filter error: {e}")
        return None

# 🤖 AI: SAME EVENT DETECTION

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

# 📡 RSS 

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

# 📲 TELEGRAM CONNECTION

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

#🔄 ONE SCAN CYCLE

def run_cycle(seen_links: Set[str], seen_titles: List[str], cycle_num: int):
    logging.info(f"=== Cycle {cycle_num} starting ===")

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

            badge = TYPE_BADGES.get(news_type, DEFAULT_BADGE)
            summary_with_badge = f"{badge}\n{summary}"

            post_to_telegram(source, summary_with_badge, link)

            save_to_history(link)
            save_title_history(title)

            seen_links.add(link)
            seen_titles.append(normalize_title(title))

            time.sleep(RATE_LIMIT_SLEEP)

        time.sleep(RATE_LIMIT_SLEEP)

    logging.info(f"=== Cycle {cycle_num} complete ===")

# 🚀 MAIN CODE

def run():
    run_start = time.time()
    cycle_num = 0

    seen_links  = load_history()
    seen_titles = load_title_history()

    while True:
        elapsed = time.time() - run_start
        if elapsed >= TOTAL_RUNTIME_SECONDS:
            logging.info("Total runtime reached. Exiting.")
            break

        cycle_num += 1
        cycle_start = time.time()

        run_cycle(seen_links, seen_titles, cycle_num)

        cycle_duration = time.time() - cycle_start
        remaining_total = TOTAL_RUNTIME_SECONDS - (time.time() - run_start)

        if remaining_total <= 0:
            logging.info("Total runtime reached after cycle. Exiting.")
            break

        sleep_time = max(0, min(CYCLE_INTERVAL_SECONDS - cycle_duration, remaining_total))
        if sleep_time > 0:
            logging.info(
                f"Next cycle in {sleep_time/60:.1f} min "
                f"({remaining_total/60:.0f} min remaining in run)."
            )
            time.sleep(sleep_time)

if __name__ == "__main__":
    run()
