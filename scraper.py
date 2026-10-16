import asyncio
import json
import os
import re
import sys
import time
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

WP_URL = os.environ["WP_URL"]
WP_KEY = os.environ["WP_API_KEY"]

VPAUTO_URL = "https://www.vpauto.fr/vehicule/liste?event=43301"
MAX_DETAILS = 10  # Limite pour test (mettre à 100 plus tard)


# ============================================
# 1. Récupération de la LISTE (via Playwright)
# ============================================
async def fetch_list() -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            locale="fr-FR", timezone_id="Europe/Paris",
        )
        page = await context.new_page()
        stealth = Stealth()
        await stealth.apply_stealth_async(page)

        print(f"📥 Liste : {VPAUTO_URL}")
        await page.goto(VPAUTO_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(6)

        html = await page.content()
        await browser.close()
        print(f"   ✅ {len(html)} caractères")
        return html


# ============================================
# 2. Parse de la LISTE
# ============================================
def clean_int(text):
    if not text:
        return 0
    n = re.sub(r"[^\d]", "", str(text))
    return int(n) if n else 0


def parse_list(html):
    soup = BeautifulSoup(html, "html.parser")
    vehicles = []

    for art in soup.find_all("article", class_="element"):
        try:
            link = art.find("a", href=True)
            if not link:
                continue

            href = link["href"]
            url = "https://www.vpauto.fr" + href if href.startswith("/") else href

            ref_match = re.search(r"/vehicule/([^/]+)/", href)
            ref = ref_match.group(1) if ref_match else ""

            img = art.find("img")
            main_img = img["src"] if img and img.get("src") else ""

            marque_div = art.find("div", class_=re.compile(r"elmt-marque"))
            brand = marque_div.find("h2").get_text(strip=True) if marque_div and marque_div.find("h2") else ""

            modele_div = art.find("div", class_="elmt-modele")
            detailed_model = ""
            year = 0
            km = 0
            if modele_div:
                h3 = modele_div.find("h3")
                detailed_model = h3.get_text(strip=True) if h3 else ""
                spans = modele_div.find_all("span")
                if len(spans) >= 2:
                    year = clean_int(spans[0].get_text())
                    km = clean_int(spans[1].get_text())

            prix_div = art.find("div", class_="elmt-prix")
            price = 0
            price_label = ""
            if prix_div:
                span_prix = prix_div.find("span", class_="prix")
                price = clean_int(span_prix.get_text()) if span_prix else 0
                first_span = prix_div.find("span")
                price_label = first_span.get_text(strip=True) if first_span else ""

            saling = art.find("div", class_="vehicle-salingState")
            sale_state = ""
            final_price = 0
            if saling:
                spans = saling.find_all("span")
                sale_state = spans[0].get_text(strip=True) if spans else ""
                final_price = clean_int(spans[1].get_text()) if len(spans) > 1 else 0

            ville = art.find("span", class_="elmt-ville")
            dept = ville.get_text(strip=True) if ville else ""

            ordre = art.find("span", class_="elmt-ordre")
            ordre_num = ordre.get_text(strip=True) if ordre else ""

            vehicles.append({
                "id": ref,
                "_url": url,
                "images": [main_img] if main_img else [],
                "brand": brand,
                "detailed_model": detailed_model,
                "price": price,
                "_price_label": price_label,
                "_sale_state": sale_state,
                "_sale_final_price": final_price,
                "_dept": dept,
                "_ordre": ordre_num,
                "year": year,
                "mileage": km,
                "_img_prefix": extract_img_prefix(main_img),
            })
        except Exception as e:
            print(f"   ⚠️ Erreur parsing : {e}")
            continue

    return vehicles


def extract_img_prefix(url):
    """Extrait le préfixe d'image pour reconstruire toutes les photos."""
    m = re.search(r"/([A-Za-z0-9_-]+)_01-\d+\.jpg", url)
    return m.group(1) if m else ""


# ============================================
# 3. Récupération du DÉTAIL (via curl_cffi, rapide)
# ============================================
def fetch_detail(url):
    try:
        r = cffi_requests.get(
            url,
            impersonate="chrome",
            timeout=30,
            allow_redirects=True,
        )
        if r.status_code == 200:
            return r.text
    except Exception as e:
        print(f"      ❌ {e}")
    return ""


def parse_detail(html):
    """Extrait les infos complémentaires depuis la page détail."""
    soup = BeautifulSoup(html, "html.parser")
    extra = {}

    # Cherche la table/fiche de caractéristiques
    fiche = soup.find("div", class_=re.compile(r"fiche"))
    if fiche:
        text = fiche.get_text(" ", strip=True)

        # Énergie
        m = re.search(r"(?:Énergie|Energie)\s*:?\s*([A-Za-zéèêàôîû\s]+?)(?:\s{2,}|Boîte|Puissance|$)", text)
        if m:
            extra["fuel"] = m.group(1).strip()

        # Boîte
        m = re.search(r"(?:Boîte|Boite)(?:\s+de\s+vitesse)?\s*:?\s*([A-Za-zéèêàôîû\s]+?)(?:\s{2,}|Puissance|$)", text)
        if m:
            extra["gearbox"] = m.group(1).strip()

        # Mise en circulation
        m = re.search(r"Mise en circulation\s*:?\s*(\d{2}/\d{4}|\d{4})", text)
        if m:
            extra["first_registration"] = m.group(1)

        # Puissance
        m = re.search(r"Puissance\s*:?\s*([\d]+\s*(?:ch|kW))", text)
        if m:
            extra["_puissance"] = m.group(1)

    # Toutes les photos (via CDN)
    img_prefix = None
    main_img = soup.find("img", src=re.compile(r"cdn\.vpauto\.fr/"))
    if main_img:
        m = re.search(r"/([A-Za-z0-9_-]+)_01-\d+\.jpg", main_img["src"])
        if m:
            img_prefix = m.group(1)

    if img_prefix:
        photos = []
        for i in range(1, 20):  # Max 20 photos
            for ext in ["1200", "640", "80"]:
                url = f"https://cdn.vpauto.fr/{img_prefix}_{i:02d}-{ext}.jpg"
                photos.append(url)
                break  # On garde juste la 1200px
        extra["images"] = photos

    return extra


# ============================================
# 4. MAIN
# ============================================
async def main():
    # 1. Liste
    list_html = await fetch_list()

    if len(list_html) < 50000:
        print("❌ HTML trop petit")
        sys.exit(1)

    vehicles = parse_list(list_html)
    print(f"\n📦 {len(vehicles)} véhicules dans la liste")

    if not vehicles:
        sys.exit(0)

    # 2. Détails (seulement pour les X premiers)
    print(f"\n🔍 Récupération des détails (max {MAX_DETAILS})...")
    for i, v in enumerate(vehicles[:MAX_DETAILS], 1):
        print(f"   [{i}/{min(len(vehicles), MAX_DETAILS)}] {v['brand']} {v['detailed_model'][:30]}")
        detail_html = fetch_detail(v["_url"])
        if detail_html:
            extra = parse_detail(detail_html)
            v.update(extra)
        time.sleep(1)

    # 3. Aperçu
    print("\n--- Aperçu ---")
    for v in vehicles[:3]:
        print(f"  {v.get('brand')} | {v.get('detailed_model')[:35]} | {v.get('price')}€ | {v.get('fuel', 'N/A')} | {len(v.get('images', []))} imgs")

    # 4. Envoi à WordPress
    payload = {
        "source": "VPauto",
        "total": len(vehicles),
        "vehicules": vehicles,
    }

    print(f"\n📤 Envoi à WordPress ({len(vehicles)} véhicules)...")
    response = cffi_requests.post(
        WP_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {WP_KEY}",
            "Content-Type": "application/json",
        },
        timeout=120,
        impersonate="chrome",
    )

    if response.status_code == 200:
        print(f"✅ WordPress : {response.json()}")
    else:
        print(f"❌ Erreur ({response.status_code}) : {response.text[:300]}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
