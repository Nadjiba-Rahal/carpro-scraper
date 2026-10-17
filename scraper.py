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
MAX_DETAILS = 100


async def fetch_list():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            locale="fr-FR", timezone_id="Europe/Paris")
        page = await ctx.new_page()
        stealth = Stealth()
        await stealth.apply_stealth_async(page)
        print(f"📥 Liste : {VPAUTO_URL}")
        await page.goto(VPAUTO_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(6)
        html = await page.content()
        await browser.close()
        print(f"   ✅ {len(html)} caractères")
        return html


def clean_int(t):
    if not t: return 0
    n = re.sub(r"[^\d]", "", str(t))
    return int(n) if n else 0


def parse_list(html):
    soup = BeautifulSoup(html, "html.parser")
    vehicles = []
    for art in soup.find_all("article", class_="element"):
        try:
            link = art.find("a", href=True)
            if not link: continue
            href = link["href"]
            url = "https://www.vpauto.fr" + href if href.startswith("/") else href
            ref_m = re.search(r"/vehicule/([^/]+)/", href)
            ref = ref_m.group(1) if ref_m else ""

            img = art.find("img")
            main_img = img["src"] if img and img.get("src") else ""

            marque_div = art.find("div", class_=re.compile(r"elmt-marque"))
            brand = marque_div.find("h2").get_text(strip=True) if marque_div and marque_div.find("h2") else ""

            modele_div = art.find("div", class_="elmt-modele")
            detailed_model = ""; year = 0; km = 0
            if modele_div:
                h3 = modele_div.find("h3")
                detailed_model = h3.get_text(strip=True) if h3 else ""
                spans = modele_div.find_all("span")
                if len(spans) >= 2:
                    year = clean_int(spans[0].get_text())
                    km = clean_int(spans[1].get_text())

            prix_div = art.find("div", class_="elmt-prix")
            price = 0
            if prix_div:
                sp = prix_div.find("span", class_="prix")
                price = clean_int(sp.get_text()) if sp else 0

            saling = art.find("div", class_="vehicle-salingState")
            sale_state = ""; final_price = 0
            if saling:
                spans = saling.find_all("span")
                sale_state = spans[0].get_text(strip=True) if spans else ""
                final_price = clean_int(spans[1].get_text()) if len(spans) > 1 else 0

            ville = art.find("span", class_="elmt-ville")
            dept = ville.get_text(strip=True) if ville else ""
            ordre = art.find("span", class_="elmt-ordre")
            ordre_n = ordre.get_text(strip=True) if ordre else ""

            vehicles.append({
                "id": ref,
                "_url": url,
                "images": [main_img] if main_img else [],
                "brand": brand,
                "detailed_model": detailed_model,
                "price": price,
                "_sale_state": sale_state,
                "_sale_final_price": final_price,
                "_dept": dept,
                "_ordre": ordre_n,
                "year": year,
                "mileage": km,
            })
        except Exception as e:
            print(f"   ⚠️ {e}")
            continue
    return vehicles


def fetch_detail(url):
    try:
        r = cffi_requests.get(url, impersonate="chrome", timeout=30, allow_redirects=True)
        if r.status_code == 200: return r.text
    except: pass
    return ""


def parse_detail(html):
    """Extrait TOUTES les photos + infos depuis la page détail."""
    soup = BeautifulSoup(html, "html.parser")
    extra = {}

    # ============ IMAGES RÉELLES (toutes celles du CDN) ============
    all_imgs = soup.find_all("img")
    photos = []
    seen = set()
    for img in all_imgs:
        src = img.get("src") or img.get("data-src") or ""
        # Filtre uniquement les vraies photos de véhicule (pas les icones)
        if "cdn.vpauto.fr" in src and "-1200.jpg" in src:
            if src not in seen:
                photos.append(src)
                seen.add(src)

    # Si pas de 1200px trouvées, cherche les 640px et remplace par 1200
    if not photos:
        for img in all_imgs:
            src = img.get("src") or ""
            if "cdn.vpauto.fr" in src and re.search(r"_\d+-\d+\.jpg", src):
                # Ex: KsIFSZW_01-640.jpg → KsIFSZW_01-1200.jpg
                hi_res = re.sub(r"-\d+\.jpg", "-1200.jpg", src)
                if hi_res not in seen:
                    photos.append(hi_res)
                    seen.add(hi_res)

    if photos:
        extra["images"] = photos
        print(f"      🖼️ {len(photos)} photos")

    # ============ FICHE TECHNIQUE ============
    page_text = soup.get_text(" ", strip=True)

    # Énergie
    m = re.search(r"Énergie\s*[:\-]?\s*(Essence|Diesel|Électrique|Hybride|GPL|GNV|Hydrogène)", page_text, re.I)
    if m: extra["fuel"] = m.group(1)

    # Boîte
    m = re.search(r"Bo[îi]te(?:\s+de\s+vitesse)?\s*[:\-]?\s*(Automatique|Manuelle|Semi-auto)", page_text, re.I)
    if m: extra["gearbox"] = m.group(1)

    # Mise en circulation
    m = re.search(r"[Mm]ise\s+en\s+circulation\s*[:\-]?\s*(\d{2}/\d{2}/\d{4}|\d{2}/\d{4}|\d{4})", page_text)
    if m: extra["first_registration"] = m.group(1)

    # Puissance
    m = re.search(r"Puissance\s*[:\-]?\s*(\d+\s*(?:ch|kW|CV))", page_text, re.I)
    if m: extra["_puissance"] = m.group(1)

    # VIN / Châssis
    m = re.search(r"\b(VF[A-Z0-9]{15}|WVW[A-Z0-9]{14}|[A-HJ-NPR-Z0-9]{17})\b", page_text)
    if m: extra["vin"] = m.group(1)

    return extra


async def main():
    list_html = await fetch_list()
    if len(list_html) < 50000:
        print("❌ HTML trop petit"); sys.exit(1)

    vehicles = parse_list(list_html)
    print(f"\n📦 {len(vehicles)} véhicules")

    print(f"\n🔍 Détails (max {MAX_DETAILS})...")
    for i, v in enumerate(vehicles[:MAX_DETAILS], 1):
        print(f"   [{i}] {v['brand']} {v['detailed_model'][:30]}")
        dh = fetch_detail(v["_url"])
        if dh:
            extra = parse_detail(dh)
            v.update(extra)
        time.sleep(1)

    print("\n--- Aperçu ---")
    for v in vehicles[:3]:
        print(f"  {v.get('brand')} | {v.get('detailed_model')[:30]} | {v.get('price')}€ | {v.get('fuel','N/A')} | {len(v.get('images',[]))} imgs")

    payload = {"source": "VPauto", "total": len(vehicles), "vehicules": vehicles}
    print(f"\n📤 Envoi à WordPress ({len(vehicles)})...")
    r = cffi_requests.post(WP_URL, json=payload,
        headers={"Authorization": f"Bearer {WP_KEY}", "Content-Type": "application/json"},
        timeout=120, impersonate="chrome")
    print(f"✅ {r.json()}" if r.status_code == 200 else f"❌ {r.status_code}: {r.text[:300]}")


if __name__ == "__main__":
    asyncio.run(main())
