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
BATCH_SIZE = 3
MIN_IMAGES = 3  # Skip les véhicules sans image


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
        except Exception:
            continue
    return vehicles


def fetch_detail(url, retries=2):
    for attempt in range(retries + 1):
        try:
            r = cffi_requests.get(url, impersonate="chrome", timeout=30, allow_redirects=True)
            if r.status_code == 200 and len(r.text) > 50000:  # ← Check taille
                return r.text
        except Exception:
            pass
        time.sleep(2)
    return ""


def parse_detail(html):
    """Extrait photos + infos techniques."""
    soup = BeautifulSoup(html, "html.parser")
    extra = {}

    # ============ PHOTOS ============
    all_imgs = soup.find_all("img")
    photos = []
    seen = set()

    # Priorité aux 1200px
    for img in all_imgs:
        src = img.get("src") or img.get("data-src") or ""
        if "cdn.vpauto.fr" in src and "-1200.jpg" in src:
            if src not in seen:
                photos.append(src)
                seen.add(src)

    # Fallback : upgrade des 640px → 1200px
    if not photos:
        for img in all_imgs:
            src = img.get("src") or ""
            if "cdn.vpauto.fr" in src and re.search(r"_\d+-\d+\.jpg", src):
                hi_res = re.sub(r"-\d+\.jpg", "-1200.jpg", src)
                if hi_res not in seen:
                    photos.append(hi_res)
                    seen.add(hi_res)

    if photos:
        extra["images"] = photos

    # ============ INFOS TECHNIQUES ============
    page_text = soup.get_text(" ", strip=True)

    # Énergie
    for p in [r"Énergie\s*[:\-]?\s*(Essence|Diesel|Électrique|Electrique|Hybride|Hybride rechargeable|GPL|GNV|Hydrogène)",
              r"Carburant\s*[:\-]?\s*(Essence|Diesel|Électrique|Electrique|Hybride|GPL|GNV)",
              r"\b(Essence|Diesel|Électrique|Electrique|Hybride rechargeable|Hybride)\b"]:
        m = re.search(p, page_text, re.I)
        if m:
            extra["fuel"] = m.group(1).strip()
            break

    # Boîte
    for p in [r"Bo[îi]te(?:\s+de\s+vitesse)?\s*[:\-]?\s*(Automatique|Manuelle|Semi-automatique)",
              r"\b(Automatique|Manuelle)\b"]:
        m = re.search(p, page_text, re.I)
        if m:
            extra["gearbox"] = m.group(1).strip()
            break

    # Mise en circulation
    m = re.search(r"[Mm]ise\s+en\s+circulation\s*[:\-]?\s*(\d{2}/\d{2}/\d{4}|\d{2}/\d{4}|\d{4})", page_text)
    if m: extra["first_registration"] = m.group(1)

    # Puissance
    m = re.search(r"Puissance\s*[:\-]?\s*(\d+\s*(?:ch|kW|CV))", page_text, re.I)
    if m: extra["_puissance"] = m.group(1)

    # VIN
    m = re.search(r"\b([A-HJ-NPR-Z0-9]{17})\b", page_text)
    if m: extra["vin"] = m.group(1)

    return extra


def send_batch(vehicles, batch_num, total_batches):
    """Envoie un batch à WordPress avec retry."""
    payload = {"source": "VPauto", "total": len(vehicles), "vehicules": vehicles}

    print(f"\n📤 Batch {batch_num}/{total_batches} ({len(vehicles)} véhicules)...")

    for attempt in range(3):
        try:
            r = cffi_requests.post(
                WP_URL, json=payload,
                headers={"Authorization": f"Bearer {WP_KEY}", "Content-Type": "application/json"},
                timeout=180, impersonate="chrome")
            if r.status_code == 200:
                print(f"   ✅ {r.json()}")
                return True
            else:
                print(f"   ⚠️ Tentative {attempt+1}/3 : HTTP {r.status_code}")
        except Exception as e:
            print(f"   ⚠️ Tentative {attempt+1}/3 : {e}")
        time.sleep(5 * (attempt + 1))
    return False


async def main():
    # 1. Liste
    list_html = await fetch_list()
    if len(list_html) < 50000:
        print("❌ HTML trop petit"); sys.exit(1)

    all_vehicles = parse_list(list_html)
    print(f"\n📦 {len(all_vehicles)} véhicules dans la liste")

    if not all_vehicles:
        sys.exit(0)

    # 2. Détails + filtre véhicules AVEC images
    print(f"\n🔍 Extraction des détails (filtre : ≥ {MIN_IMAGES} image)...")
    valid_vehicles = []
    skipped_no_img = 0
    skipped_no_detail = 0

    for i, v in enumerate(all_vehicles, 1):
    dh = fetch_detail(v["_url"], retries=3)  # Plus de retry
    
    # Vérifie que le HTML est assez gros (sinon retry)
    if dh and len(dh) < 50000:
        print(f"   [{i:02d}] ⚠️  HTML trop petit, retry...")
        time.sleep(5)
        dh = fetch_detail(v["_url"], retries=3)
        if not dh:
            print(f"   [{i:02d}] ❌ {v['brand']} {v['detailed_model'][:30]} → détail KO")
            skipped_no_detail += 1
            time.sleep(0.3)
            continue

        extra = parse_detail(dh)
        v.update(extra)

        img_count = len(v.get("images", []))
        if img_count >= MIN_IMAGES:
            print(f"   [{i:02d}] ✅ {v['brand']} {v['detailed_model'][:30]} → 🖼️ {img_count}")
            valid_vehicles.append(v)
        else:
            print(f"   [{i:02d}] ⏭️  {v['brand']} {v['detailed_model'][:30]} → pas d'image (skip)")
            skipped_no_img += 1

        time.sleep(0.8)

    # 3. Résumé
    print(f"\n{'='*60}")
    print(f"📊 Résumé :")
    print(f"   Total liste       : {len(all_vehicles)}")
    print(f"   ✅ Valides        : {len(valid_vehicles)} (avec images)")
    print(f"   ⏭️  Sans image     : {skipped_no_img}")
    print(f"   ❌ Détail KO       : {skipped_no_detail}")
    print(f"{'='*60}")

    if not valid_vehicles:
        print("\n⚠️ Aucun véhicule avec images, arrêt.")
        sys.exit(0)

    # 4. Aperçu
    print(f"\n--- Aperçu 3 premiers ---")
    for v in valid_vehicles[:3]:
        print(f"   {v['brand']:<12} | {v['detailed_model'][:35]:<35} | {v['price']:>7}€ | {len(v['images'])} imgs | {v.get('fuel', 'N/A')}")

    # 5. Envoi par batch
    total_batches = (len(valid_vehicles) + BATCH_SIZE - 1) // BATCH_SIZE
    success_count = 0

    for i in range(0, len(valid_vehicles), BATCH_SIZE):
        batch = valid_vehicles[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        if send_batch(batch, batch_num, total_batches):
            success_count += len(batch)
        time.sleep(3)

    print(f"\n🎯 Envoyés : {success_count}/{len(valid_vehicles)}")
    if success_count == 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
