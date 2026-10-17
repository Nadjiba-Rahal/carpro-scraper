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
WP_BASE = WP_URL.rsplit('/carpro/', 1)[0]

VPAUTO_HOME = "https://www.vpauto.fr/"
BATCH_SIZE = 5
MIN_IMAGES = 6

# === RATE LIMITING / ROTATION ===
MAX_EVENTS_PER_RUN = 3
DELAY_BETWEEN_EVENTS = 4
DELAY_BETWEEN_DETAILS = 2.0
DETAIL_RETRIES = 3
SKIP_IF_RECENT_HOURS = 4
MAX_PARTIAL_RATE = 0.7  # abort si >70% pages partielles


# ==========================================
# API WP helpers
# ==========================================
def api_get_scraped():
    """Récupère les events scrapés récemment"""
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
        print(f"   ⚠️ scraped-events: {e}")
    return {}


def api_send(payload):
    """Envoie un batch à WP"""
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
# Extraction date fin
# ==========================================
def extract_sale_end(soup):
    page_text = soup.get_text(" ", strip=True)

    # Countdown "X Jours Y Heures Z Minutes W Secondes"
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

    # "Le DD/MM/YY" + "Ouverture du live à HH:MM"
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

    # JSON-LD
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
def fetch_curl(url, min_size=40000, retries=3, require_images=False):
    for i in range(retries + 1):
        try:
            r = cffi_requests.get(url, impersonate="chrome", timeout=30, allow_redirects=True)
            if r.status_code == 200 and len(r.text) > min_size:
                if require_images:
                    # Tolérant : n'importe quelle image vpauto
                    n = len(re.findall(
                        r'https?://[^"\'\s]*vpauto[^"\'\s]*\.(?:jpg|jpeg|png|webp)',
                        r.text, re.I
                    ))
                    if n < 5:
                        wait = 2 + (i * 2)
                        print(f"      ⚠️ Page partielle ({n} imgs), retry dans {wait}s")
                        time.sleep(wait)
                        continue
                return r.text
        except Exception:
            pass
        time.sleep(1 + i)
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
# Parse détail
# ==========================================
def parse_detail(html):
    soup = BeautifulSoup(html, "html.parser")
    extra = {}

    # IMAGES — multi-CDN
    photos, seen = [], set()

    def is_vp_photo(u):
        if not u: return False
        if any(x in u.lower() for x in ['/logo', '/icon', '/flag', '/avatar', 'sprite', 'placeholder', 'critair', 'badge', '/bundles/']):
            return False
        return 'vpauto' in u and any(u.lower().endswith(e) for e in ['.jpg', '.jpeg', '.png', '.webp'])

    for img in soup.find_all("img"):
        for attr in ['src', 'data-src', 'data-original', 'data-large', 'data-zoom']:
            src = img.get(attr) or ""
            if is_vp_photo(src) and src not in seen:
                photos.append(src); seen.add(src)
        srcset = img.get("srcset") or ""
        for part in srcset.split(","):
            u = part.strip().split(" ")[0]
            if is_vp_photo(u) and u not in seen:
                photos.append(u); seen.add(u)

    # Dédup par préfixe + upgrade 1200px
    grouped, others = {}, []
    for u in photos:
        m = re.search(r'([A-Za-z0-9_-]+)_(\d+)-(\d+)\.(?:jpg|jpeg|png|webp)$', u, re.I)
        if m:
            prefix = m.group(1) + "_" + m.group(2)
            size = int(m.group(3))
            if prefix not in grouped or size > grouped[prefix][1]:
                grouped[prefix] = (u, size)
        else:
            others.append(u)

    final = [u for _, (u, _) in sorted(grouped.items())] + others
    final = [re.sub(r'-\d+\.(jpg|jpeg|png|webp)$', r'-1200.\1', u, flags=re.I) for u in final]

    if final:
        extra["images"] = final

    # INFOS TECHNIQUES
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    def find(p, g=1):
        m = re.search(p, text, re.I)
        return m.group(g).strip() if m else ""

    for k, p in {
        "fuel": r"Energie\s*:\s*(Essence|Diesel|Électrique|Electrique|Hybride|Hybride rechargeable|GPL|GNV|Hydrogène)",
        "gearbox": r"Type\s+de\s+boite\s*:\s*(Automatique|Manuelle|Semi-automatique)",
        "first_registration": r"Mise\s+en\s+circulation\s*:\s*(\d{2}/\d{2}/\d{4})",
        "cylindree": r"Cylindrée\s*:\s*(\d+)",
        "puissance_fiscale": r"CV\s*\(fiscaux?\)\s*:\s*(\d+)",
        "puissance_moteur": r"Puissance\s*\(ch\)\s*:\s*(\d+)",
        "tva_oui_non": r"TVA\s*:\s*(Oui|Non)",
        "nombre_portes": r"Nombre\s+de\s+portes\s*:\s*(\d+)",
        "places": r"Nombre\s+de\s+places(?:\s+carte\s+grise)?\s*:\s*(\d+)",
        "nombre_vitesses": r"(?:Nbre|Nombre)\s+de\s+vitesses?\s*:\s*(\d+)",
        "largeur_cm": r"Largeur\s*\(cm\)\s*:\s*(\d+)",
        "longueur_cm": r"Longueur\s*\(cm\)\s*:\s*(\d+)",
        "hauteur_cm": r"Hauteur\s*\(cm\)\s*:\s*(\d+)",
        "co2": r"CO2\s*\(g/km\)\s*:\s*(\d+)",
        "crit_air": r"Crit'Air\s*:?\s*(\d)",
        "norme_euro": r"Norme\s+Euro\s*:\s*(\d)",
        "carnet_entretien": r"Carnet\s+d'Entretien\s*:\s*(Oui|Non)",
        "suivi_entretien": r"Suivi\s+d'Entretien\s*:\s*(Oui|Non)",
        "genre": r"Genre\s*:\s*(\w+)",
        "vin": r"\b([A-HJ-NPR-Z0-9]{17})\b",
    }.items():
        v = find(p)
        if v: extra[k] = v

    couleur = find(r"Couleur\s*:\s*([A-Za-zÀ-ÿ\s\-']+?)(?:\s+TVA|\s+Carrosserie|$)")
    if couleur: extra["couleur"] = couleur.strip()

    carrosserie = find(r"Carrosserie\s*:\s*([A-Za-zÀ-ÿ\s\-']+?)(?:\s+Carnet|\s+CO2|$)")
    if carrosserie: extra["carrosserie"] = carrosserie.strip()

    cote = find(r"Cote\s*:\s*([\d\s]+)\s*€")
    if cote: extra["cote"] = clean_int(cote)
    prix_neuf = find(r"Prix\s+neuf\s*:\s*([\d\s]+)\s*€")
    if prix_neuf: extra["prix_neuf"] = clean_int(prix_neuf)
    mise_prix = find(r"Mise\s+à\s+prix\s*([\d\s]+)\s*€")
    if mise_prix: extra["mise_a_prix"] = clean_int(mise_prix)

    # Défauts
    obs = re.search(r"Observation\(s\)\s*(.+?)(?:Equipements|Équipements|Caractéristiques|Etat\s+du|$)", text, re.I)
    if obs:
        o = re.sub(r"\s*-\s*", " • ", obs.group(1).strip()).strip(" •")
        if o: extra["defauts_signales"] = o

    # Équipements
    eq = re.search(r"(?:Equipements|Équipements)\s*/?\s*Options?\s*(.+?)(?:Etat\s+du|Caractéristiques|$)", text, re.I)
    if eq:
        items = [e.strip() for e in re.split(r"[.;•]|\s{2,}", eq.group(1)) if len(e.strip()) > 5]
        if items: extra["equipements"] = " | ".join(items)

    return extra


# ==========================================
# Traitement d'un event
# ==========================================
def process_event(code, url, terminated_events):
    print(f"\n   📋 Vente {code}")
    html = fetch_curl(url, min_size=40000, retries=2)
    if not html:
        print(f"   ❌ Fetch échoué")
        return {"action": "fetch_failed"}

    soup = BeautifulSoup(html, "html.parser")
    sale_end = extract_sale_end(soup)
    print(f"   ⏰ Fin : {sale_end or 'non trouvée'}")

    # Skip si terminée
    if sale_end:
        try:
            fin_dt = datetime.datetime.strptime(sale_end, "%Y-%m-%d %H:%M:%S")
            if fin_dt < datetime.datetime.utcnow():
                print(f"   ⏹️ Terminée (date passée) → cleanup")
                terminated_events.append(code)
                return {"action": "skipped_past"}
        except Exception:
            pass

    vehicles = parse_list(html)
    print(f"   📦 {len(vehicles)} véhicules")
    if not vehicles:
        return {"action": "empty"}

    non_sold = [v for v in vehicles if (v.get("_sale_state") or "").lower()
                not in ("adjugé", "adjuge", "vendu", "terminé", "termine")]

    if not non_sold:
        print(f"   ⏹️ Tous vendus → cleanup")
        terminated_events.append(code)
        return {"action": "skipped_terminated"}

    print(f"   ✅ Actif ({len(non_sold)}/{len(vehicles)})")

    valid = []
    partial = 0
    total = 0
    aborted = False

    for i, v in enumerate(non_sold, 1):
        total += 1
        dh = fetch_curl(v["_url"], min_size=50000, retries=DETAIL_RETRIES, require_images=True)

        n_img = 0
        if dh:
            v.update(parse_detail(dh))
            n_img = len(v.get("images", []))

        if n_img < MIN_IMAGES:
            partial += 1

        if n_img >= MIN_IMAGES:
            v["_fin_enchere"] = sale_end
            v["_event_id"] = code
            valid.append(v)
            print(f"      [{i:02d}] ✅ {v['brand']} {v['detailed_model'][:30]} → {n_img} img")
        else:
            print(f"      [{i:02d}] ⏭️ {v['brand']} {v['detailed_model'][:30]} → {n_img} img")

        if total >= 10 and (partial / total) > MAX_PARTIAL_RATE:
            print(f"   ⚠️ {partial}/{total} pages partielles → abort (rate limit)")
            aborted = True
            break

        time.sleep(DELAY_BETWEEN_DETAILS)

    print(f"   📊 Importables: {len(valid)}/{len(non_sold)}" + (" (aborted)" if aborted else ""))

    if valid:
        # Cleanup SEULEMENT si pas aborted (sinon risque de trash des valides)
        result = api_send({
            "source": "VPauto",
            "event_id": code,
            "vehicules": valid,
            "active_refs": [v["id"] for v in vehicles],
            "cleanup": not aborted,
            "terminated_events": [],
        })
        print(f"   ✅ WP: {result}")

    return {"action": "imported", "importables": len(valid), "aborted": aborted}


# ==========================================
# MAIN
# ==========================================
async def main():
    print("🏠 Fetch homepage...")
    home_html = fetch_curl(VPAUTO_HOME)
    if not home_html or len(home_html) < 30000:
        home_html = await fetch_playwright(VPAUTO_HOME)
    if not home_html:
        print("❌ Homepage inaccessible")
        sys.exit(1)
    print(f"   ✅ {len(home_html)} caractères")

    # Events
    soup = BeautifulSoup(home_html, "html.parser")
    events = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/rechercher/vente/" in href or "sale=" in href:
            m = re.search(r"/(?:vente|sale)[=/]([A-Z0-9]+)", href)
            if m:
                code = m.group(1)
                full = "https://www.vpauto.fr" + href if href.startswith("/") else href
                events[code] = full.split("#")[0]

    print(f"🎯 {len(events)} ventes détectées")

    # Scraped récemment
    scraped = api_get_scraped()
    print(f"⏭️ {len(scraped)} ventes scrapées < {SKIP_IF_RECENT_HOURS}h")

    # Filtre
    candidates = [(c, u) for c, u in events.items() if c not in scraped]
    print(f"📋 Candidats: {len(candidates)}/{len(events)}")

    if not candidates:
        print("⚠️ Toutes les ventes sont fraîches, rien à faire.")
        return

    # Traite MAX_EVENTS_PER_RUN
    to_process = candidates[:MAX_EVENTS_PER_RUN]
    print(f"▶️ Traitement: {len(to_process)} ventes (max {MAX_EVENTS_PER_RUN})")

    terminated = []
    for i, (code, url) in enumerate(to_process, 1):
        print(f"\n[{i}/{len(to_process)}] {code}")
        process_event(code, url, terminated)
        if i < len(to_process):
            time.sleep(DELAY_BETWEEN_EVENTS)

    # Cleanup des terminées
    if terminated:
        print(f"\n🗑️ Cleanup {len(terminated)} terminées")
        api_send({
            "source": "VPauto", "event_id": "",
            "vehicules": [], "active_refs": [],
            "cleanup": False, "terminated_events": terminated,
        })

    print(f"\n🎯 TERMINÉ")


if __name__ == "__main__":
    asyncio.run(main())
