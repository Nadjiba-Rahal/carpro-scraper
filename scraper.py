import asyncio
import datetime
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

# ⚙️ CONFIG
VPAUTO_URL = "https://www.vpauto.fr/vehicule/liste?event=43301"
BATCH_SIZE = 3
MIN_IMAGES = 6

_m = re.search(r"event=(\d+)", VPAUTO_URL)
EVENT_ID = _m.group(1) if _m else ""


# ==========================================
# EXTRACTION DATE FIN D'ENCHÈRE
# ==========================================
def extract_sale_end(soup) -> str:
    # 1. JSON-LD Event
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict) and data.get("@type") == "Event":
                if data.get("endDate"):
                    return str(data["endDate"]).replace("+00:00", "").replace("Z", "")
        except Exception:
            pass

    # 2. Meta tags
    for prop in ["event:end_time", "og:end_time", "end_date", "auction:end"]:
        meta = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if meta and meta.get("content"):
            return meta["content"]

    # 3. Texte "Fin dans Xh Ym"
    page_text = soup.get_text(" ", strip=True)
    m = re.search(
        r"(?:Fin|Se termine)(?:\s+dans)?\s*[:\-]?\s*"
        r"(?:(\d+)\s*j(?:ours?)?\s*)?"
        r"(?:(\d+)\s*h(?:eures?)?\s*)?"
        r"(?:(\d+)\s*m(?:in(?:utes?)?)?\s*)?"
        r"(?:(\d+)\s*s(?:ec(?:ondes?)?)?)?",
        page_text, re.I,
    )
    if m and any(m.groups()):
        days = int(m.group(1) or 0)
        hours = int(m.group(2) or 0)
        mins = int(m.group(3) or 0)
        secs = int(m.group(4) or 0)
        total = days * 86400 + hours * 3600 + mins * 60 + secs
        if total > 0:
            end = datetime.datetime.utcnow() + datetime.timedelta(seconds=total)
            return end.strftime("%Y-%m-%d %H:%M:%S")

    # 4. Fallback "Vente du DD month YYYY" → 17h
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
        m2 = re.search(r"Vente\s+du\s+(\d{1,2})\s+(\w+)\s+(\d{4})", title, re.I)
        if m2:
            day, month_name, year = m2.groups()
            months = {
                "janvier": 1, "février": 2, "fevrier": 2, "mars": 3,
                "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
                "août": 8, "aout": 8, "septembre": 9, "octobre": 10,
                "novembre": 11, "décembre": 12, "decembre": 12,
            }
            month_num = months.get(month_name.lower(), 0)
            if month_num:
                return f"{year}-{month_num:02d}-{int(day):02d} 17:00:00"

    return ""


# ==========================================
# FETCH LISTE (Playwright)
# ==========================================
async def fetch_list():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="fr-FR",
            timezone_id="Europe/Paris",
        )
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
    if not t:
        return 0
    n = re.sub(r"[^\d]", "", str(t))
    return int(n) if n else 0


# ==========================================
# PARSE LISTE
# ==========================================
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
            ref_m = re.search(r"/vehicule/([^/]+)/", href)
            ref = ref_m.group(1) if ref_m else ""

            img = art.find("img")
            main_img = img["src"] if img and img.get("src") else ""

            marque_div = art.find("div", class_=re.compile(r"elmt-marque"))
            brand = (
                marque_div.find("h2").get_text(strip=True)
                if marque_div and marque_div.find("h2")
                else ""
            )

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
            if prix_div:
                sp = prix_div.find("span", class_="prix")
                price = clean_int(sp.get_text()) if sp else 0

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


# ==========================================
# FETCH DETAIL
# ==========================================
def fetch_detail(url, retries=3):
    for attempt in range(retries + 1):
        try:
            r = cffi_requests.get(
                url, impersonate="chrome", timeout=30, allow_redirects=True
            )
            if r.status_code == 200 and len(r.text) > 50000:
                return r.text
        except Exception:
            pass
        time.sleep(2)
    return ""


def parse_detail(html):
    soup = BeautifulSoup(html, "html.parser")
    extra = {}

    all_imgs = soup.find_all("img")
    photos = []
    seen = set()

    for img in all_imgs:
        src = img.get("src") or img.get("data-src") or ""
        if "cdn.vpauto.fr" in src and "-1200.jpg" in src:
            if src not in seen:
                photos.append(src)
                seen.add(src)

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

    page_text = soup.get_text(" ", strip=True)

    for p in [
        r"Énergie\s*[:\-]?\s*(Essence|Diesel|Électrique|Electrique|Hybride|Hybride rechargeable|GPL|GNV|Hydrogène)",
        r"Carburant\s*[:\-]?\s*(Essence|Diesel|Électrique|Electrique|Hybride|GPL|GNV)",
        r"\b(Essence|Diesel|Électrique|Electrique|Hybride rechargeable|Hybride)\b",
    ]:
        m = re.search(p, page_text, re.I)
        if m:
            extra["fuel"] = m.group(1).strip()
            break

    for p in [
        r"Bo[îi]te(?:\s+de\s+vitesse)?\s*[:\-]?\s*(Automatique|Manuelle|Semi-automatique)",
        r"\b(Automatique|Manuelle)\b",
    ]:
        m = re.search(p, page_text, re.I)
        if m:
            extra["gearbox"] = m.group(1).strip()
            break

    m = re.search(
        r"[Mm]ise\s+en\s+circulation\s*[:\-]?\s*(\d{2}/\d{2}/\d{4}|\d{2}/\d{4}|\d{4})",
        page_text,
    )
    if m:
        extra["first_registration"] = m.group(1)

    m = re.search(r"Puissance\s*[:\-]?\s*(\d+\s*(?:ch|kW|CV))", page_text, re.I)
    if m:
        extra["_puissance"] = m.group(1)

    m = re.search(r"\b([A-HJ-NPR-Z0-9]{17})\b", page_text)
    if m:
        extra["vin"] = m.group(1)

    return extra


# ==========================================
# ENVOI BATCH
# ==========================================
def send_batch(vehicles, active_refs, batch_num, total_batches):
    payload = {
        "source": "VPauto",
        "event_id": EVENT_ID,
        "total": len(vehicles),
        "vehicules": vehicles,
        "active_refs": active_refs,
        "cleanup": True,
    }

    print(f"\n📤 Batch {batch_num}/{total_batches} ({len(vehicles)} véhicules | {len(active_refs)} refs actives)...")

    for attempt in range(3):
        try:
            r = cffi_requests.post(
                WP_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {WP_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=180,
                impersonate="chrome",
            )
            if r.status_code == 200:
                print(f"   ✅ {r.json()}")
                return True
            else:
                print(f"   ⚠️ Tentative {attempt+1}/3 : HTTP {r.status_code}")
        except Exception as e:
            print(f"   ⚠️ Tentative {attempt+1}/3 : {e}")
        time.sleep(5 * (attempt + 1))
    return False


# ==========================================
# MAIN
# ==========================================
async def main():
    list_html = await fetch_list()
    if len(list_html) < 50000:
        print("❌ HTML trop petit")
        sys.exit(1)

    # Date de fin
    list_soup = BeautifulSoup(list_html, "html.parser")
    sale_end = extract_sale_end(list_soup)
    if sale_end:
        print(f"⏰ Fin de vente : {sale_end}")
    else:
        print("⚠️  Date de fin non trouvée (vente peut-être terminée)")

    all_vehicles = parse_list(list_html)
    print(f"\n📦 {len(all_vehicles)} véhicules dans la liste")

    if not all_vehicles:
        sys.exit(0)

    print(f"\n🔍 Extraction des détails (filtre : ≥{MIN_IMAGES} images, actif uniquement)...")
    valid_vehicles = []       # Ceux qui seront importés/MAJ
    active_refs = []          # TOUS les véhicules actifs (même <6 images) - pour cleanup
    skipped_sold = 0
    skipped_few_imgs = 0
    skipped_no_detail = 0

    for i, v in enumerate(all_vehicles, 1):
        state = (v.get("_sale_state") or "").lower()

        # Skip les vendus/adjugés (ne pas fetch le detail)
        if state in ("adjugé", "adjuge", "vendu", "terminé", "termine"):
            print(f"   [{i:02d}] ⏭️  {v['brand']} {v['detailed_model'][:30]} → {v['_sale_state']}")
            skipped_sold += 1
            continue

        # Fetch détail
        dh = fetch_detail(v["_url"], retries=3)
        if not dh:
            print(f"   [{i:02d}] ❌ {v['brand']} {v['detailed_model'][:30]} → détail KO")
            skipped_no_detail += 1
            active_refs.append(v["id"])  # on garde actif pour pas le trashed
            time.sleep(0.3)
            continue

        extra = parse_detail(dh)
        v.update(extra)

        # Re-check après parsing détail
        state_after = (v.get("_sale_state") or "").lower()
        if state_after in ("adjugé", "adjuge", "vendu"):
            print(f"   [{i:02d}] ⏭️  {v['brand']} {v['detailed_model'][:30]} → vendu (skip)")
            skipped_sold += 1
            continue

        # Ce véhicule est actif → toujours l'ajouter à active_refs
        active_refs.append(v["id"])

        # Vérifie le nombre d'images
        img_count = len(v.get("images", []))
        if img_count >= MIN_IMAGES:
            v["_fin_enchere"] = sale_end
            print(f"   [{i:02d}] ✅ {v['brand']} {v['detailed_model'][:30]} → 🖼️ {img_count}")
            valid_vehicles.append(v)
        else:
            print(f"   [{i:02d}] ⏭️  {v['brand']} {v['detailed_model'][:30]} → {img_count} img (< {MIN_IMAGES})")
            skipped_few_imgs += 1

        time.sleep(0.4)

    print(f"\n{'='*60}")
    print(f"📊 Résumé :")
    print(f"   Total liste           : {len(all_vehicles)}")
    print(f"   ✅ Valides (importés) : {len(valid_vehicles)}")
    print(f"   🛡️  Actifs (protégés)  : {len(active_refs)}")
    print(f"   ⏭️  Vendus/Adjugés     : {skipped_sold}")
    print(f"   ⏭️  < {MIN_IMAGES} images        : {skipped_few_imgs}")
    print(f"   ❌ Détail KO           : {skipped_no_detail}")
    print(f"{'='*60}")

    if not valid_vehicles:
        print("\n⚠️ Aucun véhicule valide à importer.")
        # On envoie quand même le cleanup (au cas où tout est vendu)
        if active_refs:
            send_batch([], active_refs, 1, 1)
        sys.exit(0)

    # Aperçu
    print(f"\n--- Aperçu 3 premiers ---")
    for v in valid_vehicles[:3]:
        print(
            f"   {v['brand']:<12} | {v['detailed_model'][:35]:<35} | "
            f"{v['price']:>7}€ | {len(v['images'])} imgs | {v.get('fuel', 'N/A')}"
        )

    # Envoi par batches
    total_batches = (len(valid_vehicles) + BATCH_SIZE - 1) // BATCH_SIZE
    success_count = 0

    for i in range(0, len(valid_vehicles), BATCH_SIZE):
        batch = valid_vehicles[i : i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        if send_batch(batch, active_refs, batch_num, total_batches):
            success_count += len(batch)
        time.sleep(5)

    print(f"\n🎯 Envoyés : {success_count}/{len(valid_vehicles)}")
    if success_count == 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
