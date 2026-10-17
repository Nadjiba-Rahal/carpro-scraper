import asyncio
import datetime
import json
import os
import random
import re
import sys
import time
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

WP_URL = os.environ["WP_URL"]
WP_KEY = os.environ["WP_API_KEY"]
WP_BASE = WP_URL.rsplit('/carpro/', 1)[0]

VPAUTO_HOME = "https://www.vpauto.fr/"
BATCH_SIZE = 5
MIN_IMAGES = 6

# ⚙️ CONFIG — 1 event/run pour respecter le rate limit
MAX_EVENTS_PER_RUN = 1
SKIP_IF_RECENT_HOURS = 8  # Ne retraite un event qu'après 8h (au lieu de 4h)


# ==========================================
# API WordPress
# ==========================================
def api_get_scraped():
    try:
        r = cffi_requests.get(
            f"{WP_BASE}/carpro/v1/scraped-events",
            params={"hours": SKIP_IF_RECENT_HOURS},
            headers={"Authorization": f"Bearer {WP_KEY}"},
            timeout=30, impersonate="chrome",
        )
        if r.status_code == 200:
            return r.json().get("events", {})
    except Exception as e:
        print(f"⚠️ scraped-events: {e}")
    return {}


def api_get_imported_refs():
    try:
        r = cffi_requests.get(
            f"{WP_BASE}/carpro/v1/imported-refs",
            headers={"Authorization": f"Bearer {WP_KEY}"},
            timeout=60, impersonate="chrome",
        )
        if r.status_code == 200:
            return set(r.json().get("refs", []))
    except Exception as e:
        print(f"⚠️ imported-refs: {e}")
    return set()


def api_send(payload):
    for attempt in range(3):
        try:
            r = cffi_requests.post(
                WP_URL, json=payload,
                headers={"Authorization": f"Bearer {WP_KEY}", "Content-Type": "application/json"},
                timeout=180, impersonate="chrome",
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(3 * (attempt + 1))
    return None


# ==========================================
# Date fin enchère
# ==========================================
def extract_sale_end(soup):
    page_text = soup.get_text(" ", strip=True)

    m = re.search(
        r"Ouverture\s+(?:de\s+la\s+vente|du\s+live)?\s*:?\s*"
        r"(\d+)\s*Jours?\s*(\d+)\s*Heures?\s*"
        r"(\d+)\s*Minutes?\s*(\d+)\s*Secondes?",
        page_text, re.I,
    )
    if m:
        total = (int(m.group(1)) * 86400 + int(m.group(2)) * 3600
                 + int(m.group(3)) * 60 + int(m.group(4)))
        if total > 0:
            return (datetime.datetime.utcnow() + datetime.timedelta(seconds=total)).strftime("%Y-%m-%d %H:%M:%S")

    m_date = re.search(r"Le\s+(\d{2})/(\d{2})/(\d{2,4})", page_text)
    m_time = re.search(r"Ouverture\s+du\s+live\s+à\s+(\d{1,2})[:h](\d{2})", page_text, re.I)
    if m_date:
        day, month, year_s = int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3))
        year = 2000 + year_s if year_s < 100 else year_s
        hour = int(m_time.group(1)) if m_time else 14
        minute = int(m_time.group(2)) if m_time else 0
        try:
            return datetime.datetime(year, month, day, hour, minute).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
            if isinstance(data, dict) and data.get("@type") == "Event" and data.get("endDate"):
                return str(data["endDate"]).replace("+00:00", "").replace("Z", "")
        except Exception:
            pass

    return ""


# ==========================================
# Fetch
# ==========================================
def fetch_curl(url, min_size=40000, retries=2):
    for i in range(retries + 1):
        try:
            r = cffi_requests.get(url, impersonate="chrome", timeout=30, allow_redirects=True)
            if r.status_code == 200 and len(r.text) > min_size:
                return r.text
        except Exception:
            pass
        time.sleep(2 + i)
    return ""


async def fetch_playwright(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox", "--disable-dev-shm-usage",
        ])
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="fr-FR", timezone_id="Europe/Paris",
        )
        page = await ctx.new_page()
        await Stealth().apply_stealth_async(page)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)
            return await page.content()
        except Exception:
            return ""
        finally:
            await browser.close()


# ==========================================
# Parse liste
# ==========================================
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
            detailed_model, year, km = "", 0, 0
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
            sale_state, final_price = "", 0
            if saling:
                spans = saling.find_all("span")
                sale_state = spans[0].get_text(strip=True) if spans else ""
                final_price = clean_int(spans[1].get_text()) if len(spans) > 1 else 0

            ville = art.find("span", class_="elmt-ville")
            dept = ville.get_text(strip=True) if ville else ""
            ordre = art.find("span", class_="elmt-ordre")
            ordre_n = ordre.get_text(strip=True) if ordre else ""

            vehicles.append({
                "id": ref, "_url": url,
                "images": [main_img] if main_img else [],
                "brand": brand, "detailed_model": detailed_model,
                "price": price, "_sale_state": sale_state,
                "_sale_final_price": final_price, "_dept": dept,
                "_ordre": ordre_n, "year": year, "mileage": km,
            })
        except Exception:
            continue
    return vehicles


# ==========================================
# Parse détail (TOUS fixes appliqués)
# ==========================================
def parse_detail(html):
    soup = BeautifulSoup(html, "html.parser")
    extra = {}

    # ---------- IMAGES ----------
    photos, seen = [], set()
    def is_vp(u):
        if not u: return False
        if any(x in u.lower() for x in ['/logo', '/icon', '/flag', '/avatar', 'sprite', 'placeholder', 'critair', 'badge', '/bundles/']):
            return False
        return 'vpauto' in u and any(u.lower().endswith(e) for e in ['.jpg', '.jpeg', '.png', '.webp'])

    for img in soup.find_all("img"):
        for attr in ['src', 'data-src', 'data-original', 'data-large', 'data-zoom']:
            s = img.get(attr) or ""
            if is_vp(s) and s not in seen:
                photos.append(s); seen.add(s)
        for part in (img.get("srcset") or "").split(","):
            u = part.strip().split(" ")[0]
            if is_vp(u) and u not in seen:
                photos.append(u); seen.add(u)

    grouped = {}
    for u in photos:
        m = re.search(r'([A-Za-z0-9_-]+)_(\d+)-(\d+)\.(?:jpg|jpeg|png|webp)$', u, re.I)
        if m:
            key = m.group(1) + "_" + m.group(2)
            sz = int(m.group(3))
            if key not in grouped or sz > grouped[key][1]:
                grouped[key] = (u, sz)
    final = [u for _, (u, _) in sorted(grouped.items())]
    final = [re.sub(r'-\d+\.(jpg|jpeg|png|webp)$', r'-1200.\1', u, flags=re.I) for u in final]
    if final: extra["images"] = final

    # ---------- TEXTE ----------
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    def find(p, g=1):
        m = re.search(p, text, re.I)
        return m.group(g).strip() if m else ""

    # ---------- ÉNERGIE ----------
    info_match = re.search(
        r"Informations\s+générales(.+?)(?:Observation|Equipements|Équipements|Etat\s+du|Caractéristiques|$)",
        text, re.I
    )
    info_text = info_match.group(1) if info_match else text

    fuel_m = re.search(r"Energie\s*:\s*([A-Za-zÀ-ÿ]+)", info_text, re.I)
    if not fuel_m:
        fuel_m = re.search(r"Energie\s*:\s*([A-Za-zÀ-ÿ]+)", text, re.I)
    if fuel_m:
        fv = fuel_m.group(1).strip().lower()
        if 'électr' in fv or 'electr' in fv: extra["fuel"] = "Électrique"
        elif 'diesel' in fv: extra["fuel"] = "Diesel"
        elif 'essence' in fv: extra["fuel"] = "Essence"
        elif 'hybride' in fv: extra["fuel"] = "Hybride"
        elif 'gpl' in fv: extra["fuel"] = "GPL"
        else: extra["fuel"] = fuel_m.group(1).strip()

    # ---------- BOÎTE ----------
    gearbox = find(r"Type\s+de\s+boite\s*:\s*(Automatique|Manuelle|Semi-automatique)")
    if gearbox: extra["gearbox"] = gearbox

    # ---------- MISE EN CIRCULATION ----------
    d1 = find(r"Mise\s+en\s+circulation\s*:\s*(\d{2}/\d{2}/\d{4})")
    if d1: extra["first_registration"] = d1

    # ---------- CARACTÉRISTIQUES ----------
    for k, p in {
        "cylindree": r"Cylindrée\s*:\s*(\d+)",
        "puissance_fiscale": r"CV\s*\(fiscaux?\)\s*:\s*(\d+)",
        "puissance_moteur": r"Puissance\s*\(ch\)\s*:\s*(\d+)",
        "nombre_portes": r"Nombre\s+de\s+portes\s*:\s*(\d+)",
        "places": r"Nombre\s+de\s+places(?:\s+carte\s+grise)?\s*:\s*(\d+)",
        "nombre_vitesses": r"(?:Nbre|Nombre)\s+de\s+vitesses?\s*:\s*(\d+)",
        "largeur_cm": r"Largeur\s*\(cm\)\s*:\s*(\d+)",
        "longueur_cm": r"Longueur\s*\(cm\)\s*:\s*(\d+)",
        "hauteur_cm": r"Hauteur\s*\(cm\)\s*:\s*(\d+)",
        "co2": r"CO2\s*\(g/km\)\s*:\s*(\d+)",
        "norme_euro": r"Norme\s+Euro\s*:\s*(\d)",
        "genre": r"Genre\s*:\s*(\w+)",
        "vin": r"\b([A-HJ-NPR-Z0-9]{17})\b",
    }.items():
        v = find(p)
        if v: extra[k] = v

    tva = find(r"TVA\s*:\s*(Oui|Non)")
    if tva: extra["tva_oui_non"] = tva

    crit_air = find(r"Crit'?Air\s*:?\s*(\d)")
    if crit_air: extra["crit_air"] = crit_air

    carnet = find(r"Carnet\s+d'Entretien\s*:\s*(Oui|Non)")
    if carnet: extra["carnet_entretien"] = carnet
    suivi = find(r"Suivi\s+d'Entretien\s*:\s*(Oui|Non)")
    if suivi: extra["suivi_entretien"] = suivi

    couleur = find(r"Couleur\s*:\s*([A-Za-zÀ-ÿ\s\-']+?)(?:\s+TVA|\s+Carrosserie|$)")
    if couleur: extra["couleur"] = couleur.strip()

    carrosserie = find(r"Carrosserie\s*:\s*([A-Za-zÀ-ÿ\s\-']+?)(?:\s+Carnet|\s+CO2|$)")
    if carrosserie: extra["carrosserie"] = carrosserie.strip()

    # ---------- COTE (fix \d*) ----------
    cote_m = re.search(r"Cote\d*\s*:\s*(\d[\d\s]*?)\s*€", text, re.I)
    if cote_m:
        v = clean_int(cote_m.group(1))
        if 0 < v < 1_000_000:
            extra["cote"] = v

    prix_neuf_m = re.search(r"Prix\s+neuf\d*\s*:\s*(\d[\d\s]*?)\s*€", text, re.I)
    if prix_neuf_m:
        v = clean_int(prix_neuf_m.group(1))
        if 0 < v < 10_000_000:
            extra["prix_neuf"] = v

    mise_m = re.search(r"Mise\s+à\s+prix\s*:?\s*(\d[\d\s]{0,15})", text, re.I)
    if mise_m:
        digits = re.sub(r"\D", "", mise_m.group(1))
        if 3 <= len(digits) <= 7:
            extra["mise_a_prix"] = int(digits)

    # ---------- DÉFAUTS ----------
    obs = re.search(r"Observation\(s\)\s*(.+?)(?:Equipements|Équipements|Caractéristiques|Etat\s+du|$)", text, re.I)
    if obs:
        o = re.sub(r"\s*-\s*", " • ", obs.group(1).strip()).strip(" •")
        if o: extra["defauts_signales"] = o

    # ---------- ÉQUIPEMENTS ----------
    eq = re.search(r"(?:Equipements|Équipements)\s*/?\s*Options?\s*(.+?)(?:Etat\s+du|Caractéristiques|$)", text, re.I)
    if eq:
        items = [e.strip() for e in re.split(r"[.;•]|\s{2,}", eq.group(1)) if len(e.strip()) > 5]
        if items: extra["equipements"] = " | ".join(items)

    return extra


# ==========================================
# Fetch détails (Playwright)
# ==========================================
async def fetch_details_batch(non_sold, already_imported, sale_end, code):
    valid = []
    skipped = 0
    aborted = False
    partial = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox", "--disable-dev-shm-usage",
        ])
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="fr-FR", timezone_id="Europe/Paris",
        )
        page = await ctx.new_page()
        await Stealth().apply_stealth_async(page)

        for i, v in enumerate(non_sold, 1):
            if v["id"] in already_imported:
                v["_fin_enchere"] = sale_end
                v["_event_id"] = code
                v["images"] = []
                valid.append(v)
                skipped += 1
                print(f"      [{i:02d}] ⏭️  {v['brand']} {v['detailed_model'][:30]} (skip)")
                continue

            try:
                await page.goto(v["_url"], wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(random.uniform(3.5, 5))
                html = await page.content()
                v.update(parse_detail(html))
                n = len(v.get("images", []))
            except Exception as e:
                print(f"      [{i:02d}] ⚠️ {str(e)[:50]}")
                n = 0

            if n >= MIN_IMAGES:
                v["_fin_enchere"] = sale_end
                v["_event_id"] = code
                valid.append(v)
                print(f"      [{i:02d}] ✅ {v['brand']} {v['detailed_model'][:30]} → {n} img")
            else:
                partial += 1
                print(f"      [{i:02d}] ⏭️  {v['brand']} {v['detailed_model'][:30]} → {n} img")

            # Abort si trop de partielles
            if i >= 15 and (partial / i) > 0.7:
                print(f"   ⚠️ {partial}/{i} partielles → abort")
                aborted = True
                break

            await asyncio.sleep(random.uniform(2, 3))

        await browser.close()

    return valid, skipped, aborted


# ==========================================
# Traitement event
# ==========================================
async def process_event(code, url, terminated, already_imported):
    print(f"\n   📋 Vente {code}")
    html = fetch_curl(url, min_size=40000, retries=2)
    if not html:
        print(f"   ❌ Fetch échoué")
        return

    soup = BeautifulSoup(html, "html.parser")
    sale_end = extract_sale_end(soup)
    print(f"   ⏰ Fin : {sale_end or 'non trouvée'}")

    if sale_end:
        try:
            fin_dt = datetime.datetime.strptime(sale_end, "%Y-%m-%d %H:%M:%S")
            if fin_dt < datetime.datetime.utcnow():
                print(f"   ⏹️ Terminée → cleanup")
                terminated.append(code)
                return
        except Exception:
            pass

    vehicles = parse_list(html)
    print(f"   📦 {len(vehicles)} véhicules")
    if not vehicles: return

    non_sold = [v for v in vehicles if (v.get("_sale_state") or "").lower()
                not in ("adjugé", "adjuge", "vendu", "terminé", "termine")]
    if not non_sold:
        print(f"   ⏹️ Tous vendus")
        terminated.append(code)
        return

    already = sum(1 for v in non_sold if v["id"] in already_imported)
    print(f"   ✅ Actif ({len(non_sold)}/{len(vehicles)}) | {already} déjà importés")

    valid, skipped, aborted = await fetch_details_batch(non_sold, already_imported, sale_end, code)

    print(f"   📊 Importables: {len(valid)}/{len(non_sold)} ({skipped} skip, {len(valid)-skipped} fetch)" + (" [aborted]" if aborted else ""))

    if valid:
        batches = [valid[i:i+BATCH_SIZE] for i in range(0, len(valid), BATCH_SIZE)]
        for i, batch in enumerate(batches):
            is_last = (i == len(batches) - 1)
            result = api_send({
                "source": "VPauto",
                "event_id": code,
                "vehicules": batch,
                "active_refs": [v["id"] for v in vehicles] if is_last else [],
                "cleanup": is_last and not aborted,
                "terminated_events": [],
                "aborted": aborted,
                "complete": not aborted,
            })
            print(f"      ✅ WP batch {i+1}/{len(batches)}: {result}")
            time.sleep(2)


# ==========================================
# MAIN
# ==========================================
async def main():
    print("🏠 Fetch homepage...")
    home_html = fetch_curl(VPAUTO_HOME)
    if not home_html or len(home_html) < 30000:
        home_html = await fetch_playwright(VPAUTO_HOME)
    if not home_html:
        print("❌ Homepage inaccessible"); sys.exit(1)
    print(f"   ✅ {len(home_html)} caractères")

    soup = BeautifulSoup(home_html, "html.parser")
    events = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/rechercher/vente/" in href or "sale=" in href:
            m = re.search(r"/(?:vente|sale)[=/]([A-Z0-9]+)", href)
            if m:
                full = "https://www.vpauto.fr" + href if href.startswith("/") else href
                events[m.group(1)] = full.split("#")[0]

    print(f"🎯 {len(events)} ventes")

    scraped = api_get_scraped()
    print(f"⏭️ {len(scraped)} ventes scrapées < {SKIP_IF_RECENT_HOURS}h")

    candidates = [(c, u) for c, u in events.items() if c not in scraped]
    print(f"📋 Candidats: {len(candidates)}/{len(events)}")

    if not candidates:
        print("⚠️ Aucun candidat."); return

    # Trie par plus ancien scrapé (ou jamais)
    candidates.sort(key=lambda x: scraped.get(x[0], '0000-00-00 00:00:00'))

    already_imported = api_get_imported_refs()
    print(f"ℹ️ {len(already_imported)} refs déjà importés")

    to_process = candidates[:MAX_EVENTS_PER_RUN]
    print(f"▶️ Traitement: {len(to_process)} vente(s)")

    terminated = []
    for i, (code, url) in enumerate(to_process, 1):
        print(f"\n[{i}/{len(to_process)}] {code}")
        await process_event(code, url, terminated, already_imported)

    if terminated:
        print(f"\n🗑️ Cleanup {len(terminated)} terminées")
        api_send({
            "source": "VPauto", "event_id": "",
            "vehicules": [], "active_refs": [],
            "cleanup": False, "terminated_events": terminated,
            "aborted": False, "complete": True,
        })

    print(f"\n🎯 TERMINÉ")


if __name__ == "__main__":
    asyncio.run(main())
