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

VPAUTO_HOME = "https://www.vpauto.fr/"
BATCH_SIZE = 5
MIN_IMAGES = 6

# Délais (anti rate-limit)
DELAY_BETWEEN_EVENTS = 3
DELAY_BETWEEN_DETAILS = 1.2
DETAIL_RETRIES = 4


# ==========================================
# 🕐 EXTRACTION DATE FIN (countdown VPauto)
# ==========================================
def extract_sale_end(soup, html=""):
    """
    Cherche la date de fin d'enchère.
    VPauto affiche soit:
    - "Ouverture de la vente : X Jours Y Heures Z Minutes W Secondes" (compte à rebours)
    - "Le DD/MM/YY" + "Ouverture du live à HH:MM"
    """

    page_text = soup.get_text(" ", strip=True)

    # PRIORITÉ 1 : Countdown "X Jours Y Heures Z Minutes W Secondes"
    m = re.search(
        r"Ouverture\s+(?:de\s+la\s+vente|du\s+live)?\s*:?\s*"
        r"(\d+)\s*Jours?\s*"
        r"(\d+)\s*Heures?\s*"
        r"(\d+)\s*Minutes?\s*"
        r"(\d+)\s*Secondes?",
        page_text, re.I,
    )
    if m:
        days = int(m.group(1))
        hours = int(m.group(2))
        mins = int(m.group(3))
        secs = int(m.group(4))
        total = days * 86400 + hours * 3600 + mins * 60 + secs
        if total > 0:
            end = datetime.datetime.utcnow() + datetime.timedelta(seconds=total)
            return end.strftime("%Y-%m-%d %H:%M:%S")

    # PRIORITÉ 2 : "Le DD/MM/YY" + "Ouverture du live à HH:MM"
    m_date = re.search(r"Le\s+(\d{2})/(\d{2})/(\d{2,4})", page_text)
    m_time = re.search(r"Ouverture\s+du\s+live\s+à\s+(\d{1,2})[:h](\d{2})", page_text, re.I)
    if m_date:
        day = int(m_date.group(1))
        month = int(m_date.group(2))
        year_short = int(m_date.group(3))
        year = 2000 + year_short if year_short < 100 else year_short
        hour = int(m_time.group(1)) if m_time else 14
        minute = int(m_time.group(2)) if m_time else 0
        try:
            end = datetime.datetime(year, month, day, hour, minute, 0)
            return end.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    # PRIORITÉ 3 : JSON-LD Event
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict) and data.get("@type") == "Event" and data.get("endDate"):
                return str(data["endDate"]).replace("+00:00", "").replace("Z", "")
        except Exception:
            pass

    # PRIORITÉ 4 : Meta tags
    for prop in ["event:end_time", "og:end_time"]:
        meta = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if meta and meta.get("content"):
            return meta["content"]

    return ""


# ==========================================
# 🌐 FETCH
# ==========================================
def fetch_curl(url, min_size=40000, retries=3):
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
        stealth = Stealth()
        await stealth.apply_stealth_async(page)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)
            return await page.content()
        except Exception:
            return ""
        finally:
            await browser.close()


# ==========================================
# 🎯 EXTRACTION EVENTS (multi-format)
# ==========================================
def extract_event_urls(html):
    """Détecte /rechercher/vente/VSXXXXX et ?sale=VSXXXXX"""
    soup = BeautifulSoup(html, "html.parser")
    urls = {}

    def add(url):
        if not url:
            return
        url = url.split("#")[0]
        if url.startswith("/"):
            url = "https://www.vpauto.fr" + url
        elif not url.startswith("http"):
            url = "https://www.vpauto.fr/" + url

        # Extrait le code vente (VS000XX)
        m = re.search(r"/(?:vente|sale)[=/]([A-Z0-9]+)", url)
        if not m:
            return
        code = m.group(1)
        if code not in urls:
            urls[code] = url

    # 1. Liens <a href> vers /rechercher/vente/ ou ?sale=
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/rechercher/vente/" in href or "sale=" in href:
            add(href)

    # 2. Regex brut sur le HTML
    for m in re.finditer(r'["\']([^"\']*(?:/rechercher/vente/|sale=)[A-Z0-9]+[^"\']*)["\']', html):
        add(m.group(1))

    return urls  # dict {code: url}


def extract_event_code(url):
    m = re.search(r"/(?:vente|sale)[=/]([A-Z0-9]+)", url)
    return m.group(1) if m else ""


# ==========================================
# 📋 PARSE LISTE
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
# 🔍 PARSE DÉTAIL — TOUS LES CHAMPS
# ==========================================
def parse_detail(html):
    soup = BeautifulSoup(html, "html.parser")
    extra = {}

    # ============ IMAGES — Multi-CDN ============
    photos, seen = [], set()

    # On accepte tous les CDN VPauto (cdn.vpauto.fr, fs.vpauto.net, photos.vpauto.fr...)
    def is_vpauto_photo(url):
        if not url:
            return False
        # Exclure les logos, icônes, flags, avatars
        if any(x in url.lower() for x in [
            '/logo', '/icon', '/flag', '/avatar', 'sprite', 'placeholder',
            'critair', 'badge', '/bundles/'
        ]):
            return False
        # Doit contenir "vpauto" dans le domaine
        return 'vpauto' in url and any(url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp'])

    # Étape 1 : Chercher les images dans src, data-src, data-original, data-large
    for img in soup.find_all("img"):
        for attr in ['src', 'data-src', 'data-original', 'data-large', 'data-zoom']:
            src = img.get(attr) or ""
            if is_vpauto_photo(src) and src not in seen:
                photos.append(src)
                seen.add(src)

    # Étape 2 : Chercher dans srcset (souvent contient plusieurs tailles)
    for img in soup.find_all("img"):
        srcset = img.get("srcset") or ""
        if not srcset:
            continue
        for part in srcset.split(","):
            url = part.strip().split(" ")[0]
            if is_vpauto_photo(url) and url not in seen:
                photos.append(url)
                seen.add(url)

    # Étape 3 : Dédupliquer par préfixe (garder la plus haute résolution)
    # VPauto utilise un format : PREFIX_XX-SIZE.jpg
    # On regroupe par (PREFIX_XX) et garde le fichier le plus grand
    grouped = {}
    others = []
    for url in photos:
        m = re.search(r'([A-Za-z0-9_-]+)_(\d+)-(\d+)\.(?:jpg|jpeg|png|webp)$', url, re.I)
        if m:
            prefix = m.group(1) + "_" + m.group(2)
            size = int(m.group(3))
            if prefix not in grouped or size > grouped[prefix][1]:
                grouped[prefix] = (url, size)
        else:
            others.append(url)

    # Trie par ordre de préfixe pour garder l'ordre (01, 02, 03...)
    final_photos = [url for prefix, (url, _) in sorted(grouped.items())]
    final_photos.extend(others)

    # Étape 4 : Upgrade vers 1200px si on a des images en basse résolution
    upgraded = []
    for url in final_photos:
        if re.search(r'-\d{2,3}\.(?:jpg|jpeg|png|webp)$', url, re.I):
            # Remplace -80.jpg ou -640.jpg par -1200.jpg
            hi_res = re.sub(r'-\d+\.(jpg|jpeg|png|webp)$', r'-1200.\1', url, flags=re.I)
            upgraded.append(hi_res)
        else:
            upgraded.append(url)

    if upgraded:
        extra["images"] = upgraded

    # ============ INFOS TECHNIQUES ============
    page_text = soup.get_text(" ", strip=True)
    text_compact = re.sub(r"\s+", " ", page_text)

    def find(pattern, group=1, flags=re.I):
        m = re.search(pattern, text_compact, flags)
        return m.group(group).strip() if m else ""

    fuel = find(r"Energie\s*:\s*(Essence|Diesel|Électrique|Electrique|Hybride|Hybride rechargeable|GPL|GNV|Hydrogène)")
    if fuel: extra["fuel"] = fuel

    gearbox = find(r"Type\s+de\s+boite\s*:\s*(Automatique|Manuelle|Semi-automatique)")
    if not gearbox:
        gearbox = find(r"\b(Automatique|Manuelle)\b")
    if gearbox: extra["gearbox"] = gearbox

    d1 = find(r"Mise\s+en\s+circulation\s*:\s*(\d{2}/\d{2}/\d{4})")
    if d1: extra["first_registration"] = d1

    cyl = find(r"Cylindrée\s*:\s*(\d+)")
    if cyl: extra["cylindree"] = cyl

    cv = find(r"CV\s*\(fiscaux?\)\s*:\s*(\d+)")
    if cv: extra["puissance_fiscale"] = cv

    ch = find(r"Puissance\s*\(ch\)\s*:\s*(\d+)")
    if ch: extra["puissance_moteur"] = ch

    couleur = find(r"Couleur\s*:\s*([A-Za-zÀ-ÿ\s\-']+?)(?:\s+TVA|\s+Carrosserie|$)")
    if couleur: extra["couleur"] = couleur.strip()

    tva = find(r"TVA\s*:\s*(Oui|Non)")
    if tva: extra["tva_oui_non"] = tva

    carrosserie = find(r"Carrosserie\s*:\s*([A-Za-zÀ-ÿ\s\-']+?)(?:\s+Carnet|\s+CO2|$)")
    if carrosserie: extra["carrosserie"] = carrosserie.strip()

    portes = find(r"Nombre\s+de\s+portes\s*:\s*(\d+)")
    if portes: extra["nombre_portes"] = portes

    places = find(r"Nombre\s+de\s+places(?:\s+carte\s+grise)?\s*:\s*(\d+)")
    if places: extra["places"] = places

    vitesses = find(r"Nbre\s+de\s+vitesses\s*:\s*(\d+)") or find(r"Nombre\s+de\s+vitesses?\s*:\s*(\d+)")
    if vitesses: extra["nombre_vitesses"] = vitesses

    largeur = find(r"Largeur\s*\(cm\)\s*:\s*(\d+)")
    if largeur: extra["largeur_cm"] = largeur
    longueur = find(r"Longueur\s*\(cm\)\s*:\s*(\d+)")
    if longueur: extra["longueur_cm"] = longueur
    hauteur = find(r"Hauteur\s*\(cm\)\s*:\s*(\d+)")
    if hauteur: extra["hauteur_cm"] = hauteur

    co2 = find(r"CO2\s*\(g/km\)\s*:\s*(\d+)")
    if co2: extra["co2"] = co2
    crit_air = find(r"Crit'Air\s*:?\s*(\d)")
    if crit_air: extra["crit_air"] = crit_air
    norme = find(r"Norme\s+Euro\s*:\s*(\d)")
    if norme: extra["norme_euro"] = norme

    carnet = find(r"Carnet\s+d'Entretien\s*:\s*(Oui|Non)")
    if carnet: extra["carnet_entretien"] = carnet
    suivi = find(r"Suivi\s+d'Entretien\s*:\s*(Oui|Non)")
    if suivi: extra["suivi_entretien"] = suivi

    genre = find(r"Genre\s*:\s*(\w+)")
    if genre: extra["genre"] = genre

    cote = find(r"Cote\s*:\s*([\d\s]+)\s*€")
    if cote: extra["cote"] = clean_int(cote)

    prix_neuf = find(r"Prix\s+neuf\s*:\s*([\d\s]+)\s*€")
    if prix_neuf: extra["prix_neuf"] = clean_int(prix_neuf)

    mise_prix = find(r"Mise\s+à\s+prix\s*([\d\s]+)\s*€")
    if mise_prix: extra["mise_a_prix"] = clean_int(mise_prix)

    obs_match = re.search(
        r"Observation\(s\)\s*(.+?)(?:Equipements|Équipements|Caractéristiques|Etat\s+du\s+véhicule|$)",
        text_compact, re.I
    )
    if obs_match:
        obs_text = obs_match.group(1).strip()
        obs_text = re.sub(r"\s*-\s*", " • ", obs_text).strip(" •")
        if obs_text:
            extra["defauts_signales"] = obs_text

    eq_match = re.search(
        r"(?:Equipements|Équipements)\s*/?\s*Options?\s*(.+?)(?:Etat\s+du\s+véhicule|Caractéristiques|$)",
        text_compact, re.I
    )
    if eq_match:
        eq_text = eq_match.group(1).strip()
        equips = re.findall(r"[A-ZÀ-Ý][^A-ZÀ-Ý]+(?:[a-zà-ÿ'’\-]\s|$)", eq_text)
        if len(equips) < 2:
            equips = [e.strip() for e in re.split(r"[.;•]|\s{2,}", eq_text) if len(e.strip()) > 5]
        if equips:
            extra["equipements"] = " | ".join([e.strip() for e in equips if e.strip()])

    etat_match = re.search(
        r"Etat\s+du\s+véhicule.*?Carrosserie\s*(.+?)(?:Réparation|Peinture|Tolerie|Remplacement|$)",
        text_compact, re.I
    )
    if etat_match:
        etat_text = etat_match.group(1).strip()
        if etat_text and len(etat_text) > 5:
            if "defauts_signales" in extra:
                extra["defauts_signales"] += " • État : " + etat_text
            else:
                extra["defauts_signales"] = "État : " + etat_text

    vin = find(r"\b([A-HJ-NPR-Z0-9]{17})\b")
    if vin: extra["vin"] = vin

    return extra


# ==========================================
# 📤 ENVOI WP
# ==========================================
def send_batch(vehicles, event_code, active_refs, cleanup, terminated_events=None):
    payload = {
        "source": "VPauto",
        "event_id": event_code or "",
        "vehicules": vehicles,
        "active_refs": active_refs or [],
        "cleanup": bool(cleanup),
        "terminated_events": terminated_events or [],
    }
    label = "vehicles" if vehicles else "cleanup"
    print(f"\n📤 [{label}] code={event_code or '-'} | {len(vehicles)} veh | {len(active_refs or [])} refs | {len(terminated_events or [])} terminés")
    for attempt in range(3):
        try:
            r = cffi_requests.post(
                WP_URL, json=payload,
                headers={"Authorization": f"Bearer {WP_KEY}", "Content-Type": "application/json"},
                timeout=180, impersonate="chrome",
            )
            if r.status_code == 200:
                print(f"   ✅ {r.json()}")
                return True
            print(f"   ⚠️ HTTP {r.status_code}")
        except Exception as e:
            print(f"   ⚠️ {e}")
        time.sleep(5 * (attempt + 1))
    return False


# ==========================================
# 📋 TRAITEMENT D'UNE VENTE
# ==========================================
def process_event(event_code, event_url, all_active, terminated_events):
    print(f"\n   📋 Vente {event_code or 'AUTO'}")
    event_html = fetch_curl(event_url)
    if not event_html:
        print(f"   ❌ Fetch échoué")
        return

    event_soup = BeautifulSoup(event_html, "html.parser")
    sale_end = extract_sale_end(event_soup)
    print(f"   ⏰ Fin : {sale_end or 'non trouvée'}")

    vehicles = parse_list(event_html)
    print(f"   📦 {len(vehicles)} véhicules")
    if not vehicles:
        return

    non_sold = [v for v in vehicles if (v.get("_sale_state") or "").lower()
                not in ("adjugé", "adjuge", "vendu", "terminé", "termine")]

    if not non_sold:
        print(f"   ⏹️ Tous vendus")
        if event_code:
            terminated_events.append(event_code)
        return

    print(f"   ✅ Actif ({len(non_sold)}/{len(vehicles)})")

    valid = []
    detail_ok = 0
    detail_ko = 0

    for i, v in enumerate(non_sold, 1):
        detail_html = fetch_curl(v["_url"], min_size=50000, retries=DETAIL_RETRIES)

        if detail_html:
            extra = parse_detail(detail_html)
            v.update(extra)
            detail_ok += 1
        else:
            detail_ko += 1

        if (v.get("_sale_state") or "").lower() in ("adjugé", "adjuge", "vendu"):
            continue

        n_img = len(v.get("images", []))
        if n_img >= MIN_IMAGES:
            v["_fin_enchere"] = sale_end
            v["_event_id"] = event_code
            valid.append(v)
            print(f"      [{i:02d}] ✅ {v['brand']} {v['detailed_model'][:30]} → {n_img} img")
        else:
            print(f"      [{i:02d}] ⏭️ {v['brand']} {v['detailed_model'][:30]} → {n_img} img")

        time.sleep(DELAY_BETWEEN_DETAILS)

    print(f"   📊 Détails : {detail_ok} OK / {detail_ko} KO / {len(valid)} importables")

    if valid:
        all_active.append({
            "event_id": event_code,
            "vehicles": valid,
            "active_refs": [v["id"] for v in vehicles],
        })


# ==========================================
# 🚀 MAIN
# ==========================================
async def main():
    print("🏠 Fetch homepage...")
    home_html = fetch_curl(VPAUTO_HOME)
    if not home_html or len(home_html) < 30000:
        print("   → Playwright...")
        home_html = await fetch_playwright(VPAUTO_HOME)
    if not home_html:
        print("❌ Homepage inaccessible")
        sys.exit(1)
    print(f"   ✅ {len(home_html)} caractères")

    events = extract_event_urls(home_html)
    print(f"\n🎯 {len(events)} ventes détectées")

    if not events:
        print("⚠️ Aucune vente détectée")
        sys.exit(0)

    # Affiche les codes trouvés
    for code, url in list(events.items())[:10]:
        print(f"   {code} → {url}")

    all_active = []
    terminated_events = []

    for idx, (code, url) in enumerate(events.items(), 1):
        print(f"\n[{idx}/{len(events)}] {code}")
        process_event(code, url, all_active, terminated_events)
        time.sleep(DELAY_BETWEEN_EVENTS)

    # RÉSUMÉ
    print(f"\n{'='*60}")
    print(f"📊 RÉSUMÉ")
    print(f"   Ventes traitées   : {len(events)}")
    print(f"   ✅ Actives         : {len(all_active)}")
    print(f"   ⏹️ Terminées       : {len(terminated_events)}")
    total = sum(len(e["vehicles"]) for e in all_active)
    print(f"   📦 Véhicules valides : {total}")
    print(f"{'='*60}")

    if not all_active and not terminated_events:
        print("\n⚠️ Rien à importer.")
        sys.exit(0)

    # ENVOI
    for ev in all_active:
        vehicles = ev["vehicles"]
        event_code = ev["event_id"]
        active_refs = ev["active_refs"]
        batches = [vehicles[i:i+BATCH_SIZE] for i in range(0, len(vehicles), BATCH_SIZE)]
        for i, batch in enumerate(batches):
            is_last = (i == len(batches) - 1)
            send_batch(batch, event_code, active_refs if is_last else [], is_last)
            time.sleep(3)

    if terminated_events:
        print(f"\n🗑️ Cleanup {len(terminated_events)} ventes terminées")
        send_batch([], "", [], False, terminated_events)

    print(f"\n🎯 TERMINÉ")


if __name__ == "__main__":
    asyncio.run(main())
