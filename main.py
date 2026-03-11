import urllib.request
import urllib.parse
import json
import xml.etree.ElementTree as ET
import time
import os

# --- 🔐 PRIVATE CONFIGURATION ---
# In GitHub, we will use 'Secrets' to store these safely
TELEGRAM_TOKEN = os.getenv("8529976701:AAErv23MUiFWKk45STve3vPlfrkaCMXmzwY")
GEMINI_API_KEY = os.getenv("AIzaSyBKdf7GFNAva2ZQPaLrPeUm8bb5ixQ54-0")
CHAT_ID = "-1003778621579"
HISTORY_FILE = "posted_links.txt"

COUNTRIES = ["United States", "Moldova", "Romania", "United Kingdom", "Netherlands", "Belgium", "Ukraine"]

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return set(f.read().splitlines())
    return set()

def save_history(link):
    with open(HISTORY_FILE, "a") as f:
        f.write(link + "\n")

def ask_ai_geopolitics(title, desc, country):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    prompt = f"Geopolitical analysis for Kamorka channel. News from {country}: {title}. Summarize in 1 sharp sentence. If sports/gossip, say IGNORE."
    data = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        req = urllib.request.Request(url, data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req) as response:
            res = json.loads(response.read().decode())
            text = res['candidates'][0]['content']['parts'][0]['text']
            return None if "IGNORE" in text else text
    except:
        return f"Country: {country}\nSummary: {title}"

def post_to_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    urllib.request.urlopen(req)

def run():
    history = load_history()
    for country in COUNTRIES:
        query = f"{country} politics"
        rss_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=en-US&gl=US&ceid=US:en"
        
        try:
            req = urllib.request.Request(rss_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response:
                root = ET.fromstring(response.read())
            
            item = root.find('./channel/item')
            if item is not None:
                link = item.find('link').text
                if link not in history:
                    title = item.find('title').text
                    analysis = ask_ai_geopolitics(title, "", country)
                    if analysis:
                        post_to_telegram(f"🏛️ *Kamorka Alert*\n\n{analysis}\n\n🔗 [Link]({link})")
                        save_history(link)
                        print(f"Posted {country}")
        except Exception as e:
            print(f"Error {country}: {e}")

if __name__ == "__main__":
    run()