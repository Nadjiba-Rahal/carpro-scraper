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


# ==========================================
# DATE FIN D'ENCHÈRE
# ==========================================
def extract_sale_end(soup) -> str:
    # 1. JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict) and data.get("@type") == "Event":
                if data.get("endDate"):
                    return str(data["endDate"]).replace("+00:00", "").replace("Z", "")
        except Exception:
            pass

    # 2. Meta
    for prop in ["event:end_time", "og:end_time", "end_date", "auction:end"]:
        meta = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if meta and meta.get("content"):
            return meta["content"]

    # 3. Texte "Fin dans Xh Ym"
    page_text = soup.get_text(" ", strip=True)
    m = re.search(
        r"(?:Fin|Se termine)(?:\s+(?:de\s+la\s+vente|le|dans))?\s*[:\-]?\s*(?:dans\s+)?"
        r"(?:(\d+)\s*j(?:ours?)?\s*)?(?:(\d+)\s*h(?:eures?)?\s*)?"
        r"(?:(\d+)\s*m(?:in(?:utes?)?)?\s*)?(?:(\d+)\s*s(?:ec(?:ondes?)?)?)?",
        page_text, re.I,
    )
    if m and any(m.groups()):
        total = (int(m.group(1) or 0) * 86400 + int(m.group(2) or 0) * 3600
                 + int(m.group(3) or 0) * 60 + int(m.group(4) or 0))
        if total > 0:
            end = datetime.datetime.utcnow() + datetime.timedelta(seconds=total)
            return end.strftime("%Y-%m-%d %H:%M:%S")

    # 4. Texte "Vente du DD month YYYY"
    months_fr = {
        "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
        "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
        "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
    }
    for tag in [soup.find("h1"), soup.find("h2"), soup.find("title")]:
        if not tag:
            continue
        text = tag.get_text(strip=True)
        m2 = re.search(r"(\d{1,2})\s+(" + "|".join(months_fr.keys()) + r")\s+(\d{4})", text, re.I)
        if m2:
            day = int(m2.group(1))
            month = months_fr.get(m2.group(2).lower(), 0)
            year = int(m2.group(3))
            if month:
                try:
                    candidate = datetime.datetime(year, month, day, 17, 0, 0)
                    if candidate > datetime.datetime.utcnow() - datetime.timedelta(days=1):
                        return candidate.strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
    return ""


# ==========================================
# FETCH
# ==========================================
def fetch_curl(url, min_size=40000, retries=3):
    for _ in range(retries + 1):
        try:
            r = cffi_requests.get(url, impersonate="chrome", timeout=30, allow_redirects=True)
            if r.status_code == 200 and len(r.text) > min_size:
                return r.text
        except Exception:
            pass
        time.sleep(2)
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
            html = await page.content()
            return html
        except Exception as e:
            print(f"   ❌ Playwright: {e}")
            return ""
        finally:
            await browser.close()


# ==========================================
# EXTRACTION EVENTS
# ==========================================
def extract_event_urls(html):
    soup = BeautifulSoup(html, "html.parser")
    urls = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "event=" in href and "/vehicule/liste" in href:
            if href.startswith("/"):
                href = "https://www.vpauto.fr" + href
            href = href.split("#")[0]
            urls.add(href)
    return sorted(urls)


def extract_event_id(url):
    m = re.search(r"event=(\d+)", url)
    return m.group(1) if m else ""


# ==========================================
# PARSE LISTE
# ==========================================
def clean_int(t):
    if not t:
        return 0
    n = re.sub(r"[^\d]", "", str(t))
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
                "_sale_final_price": final_price,
                "_dept": dept, "_ordre": ordre_n,
                "year": year, "mileage": km,
            })
        except Exception:
            continue
    return vehicles


def parse_detail(html):
    soup = BeautifulSoup(html, "html.parser")
    extra = {}
    photos, seen = [], set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "cdn.vpauto.fr" in src and "-1200.jpg" in src and src not in seen:
            photos.append(src)
            seen.add(src)
    if not photos:
        for img in soup.find_all("img"):
            src = img.get("src") or ""
            if "cdn.vpauto.fr" in src and re.search(r"_\d+-\d+\.jpg", src):
                hi = re.sub(r"-\d+\.jpg", "-1200.jpg", src)
                if hi not in seen:
                    photos.append(hi)
                    seen.add(hi)
    if photos:
        extra["images"] = photos

    page_text = soup.get_text(" ", strip=True)
    for p in [
        r"Énergie\s*[:\-]?\s*(Essence|Diesel|Électrique|Electrique|Hybride|Hybride rechargeable|GPL|GNV|Hydrogène)",
        r"\b(Essence|Diesel|Électrique|Electrique|Hybride rechargeable|Hybride)\b",
    ]:
        m = re.search(p, page_text, re.I)
        if m:
            extra["fuel"] = m.group(1).strip()
            break
    for p in [r"Bo[îi]te(?:\s+de\s+vitesse)?\s*[:\-]?\s*(Automatique|Manuelle|Semi-automatique)",
              r"\b(Automatique|Manuelle)\b"]:
        m = re.search(p, page_text, re.I)
        if m:
            extra["gearbox"] = m.group(1).strip()
            break
    m = re.search(r"[Mm]ise\s+en\s+circulation\s*[:\-]?\s*(\d{2}/\d{2}/\d{4}|\d{2}/\d{4}|\d{4})", page_text)
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
# ENVOI WORDPRESS
# ==========================================
def send_batch(vehicles, event_id, active_refs, cleanup, terminated_events=None):
    payload = {
        "source": "VPauto",
        "event_id": event_id or "",
        "vehicules": vehicles,
        "active_refs": active_refs or [],
        "cleanup": bool(cleanup),
        "terminated_events": terminated_events or [],
    }
    label = "vehicles" if vehicles else "cleanup"
    print(f"\n📤 Envoi [{label}] event={event_id or '-'} | {len(vehicles)} veh, {len(active_refs)} refs, {len(terminated_events or [])} term")
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
# MAIN — MULTI-VENTES AUTO
# ==========================================
async def main():
    print("🏠 Fetch homepage...")
    home_html = fetch_curl(VPAUTO_HOME)
    if not home_html:
        print("   curl_cffi échoue → Playwright")
        home_html = await fetch_playwright(VPAUTO_HOME)
    if not home_html or len(home_html) < 30000:
        print("❌ Homepage inaccessible")
        sys.exit(1)
    print(f"   ✅ {len(home_html)} caractères")

    event_urls = extract_event_urls(home_html)
    print(f"\n🎯 {len(event_urls)} ventes trouvées sur la homepage")

    if not event_urls:
        print("⚠️ Aucune vente détectée. La structure a peut-être changé.")
        sys.exit(0)

    all_active = []          # [{'event_id', 'vehicles', 'active_refs', 'sale_end'}]
    terminated_events = []

    for idx, event_url in enumerate(event_urls, 1):
        event_id = extract_event_id(event_url)
        print(f"\n[{idx}/{len(event_urls)}] Event {event_id}")
        print(f"   URL: {event_url}")

        event_html = fetch_curl(event_url)
        if not event_html:
            print(f"   ❌ Fetch échoué")
            continue

        event_soup = BeautifulSoup(event_html, "html.parser")
        sale_end = extract_sale_end(event_soup)
        print(f"   ⏰ Fin : {sale_end or 'non trouvée'}")

        vehicles = parse_list(event_html)
        print(f"   📦 {len(vehicles)} véhicules")

        if not vehicles:
            print(f"   ⏭️ Aucun véhicule")
            continue

        # Détection ACTIVE = au moins 1 véhicule non-Adjugé
        non_sold = [v for v in vehicles if (v.get("_sale_state") or "").lower()
                    not in ("adjugé", "adjuge", "vendu", "terminé", "termine")]

        if not non_sold:
            print(f"   ⏹️ Tous vendus → event terminé")
            terminated_events.append(event_id)
            continue

        # Vente active → traiter
        print(f"   ✅ Actif ({len(non_sold)}/{len(vehicles)} non vendus)")

        valid = []
        for i, v in enumerate(non_sold, 1):
            detail_html = fetch_curl(v["_url"], min_size=50000, retries=2)
            if detail_html:
                extra = parse_detail(detail_html)
                v.update(extra)

            # Re-vérifier après fetch détail
            if (v.get("_sale_state") or "").lower() in ("adjugé", "adjuge", "vendu"):
                continue

            if len(v.get("images", [])) >= MIN_IMAGES:
                v["_fin_enchere"] = sale_end
                v["_event_id"] = event_id
                valid.append(v)
                print(f"      [{i:02d}] ✅ {v['brand']} {v['detailed_model'][:30]} → 🖼️ {len(v['images'])}")
            else:
                print(f"      [{i:02d}] ⏭️ {v['brand']} {v['detailed_model'][:30]} → {len(v.get('images', []))} img")
            time.sleep(0.3)

        if valid:
            all_active.append({
                "event_id": event_id,
                "vehicles": valid,
                "active_refs": [v["id"] for v in vehicles],
                "sale_end": sale_end,
            })

    # ==========================================
    # RÉSUMÉ
    # ==========================================
    print(f"\n{'='*60}")
    print(f"📊 RÉSUMÉ GLOBAL")
    print(f"   Ventes trouvées    : {len(event_urls)}")
    print(f"   ✅ Ventes actives  : {len(all_active)}")
    print(f"   ⏹️ Ventes terminées: {len(terminated_events)}")
    total_vehicles = sum(len(e["vehicles"]) for e in all_active)
    print(f"   📦 Véhicules valides : {total_vehicles}")
    print(f"{'='*60}")

    # ==========================================
    # ENVOI DES VENTES ACTIVES
    # ==========================================
    for event_data in all_active:
        vehicles = event_data["vehicles"]
        event_id = event_data["event_id"]
        active_refs = event_data["active_refs"]

        # Découpe en batches
        batches = [vehicles[i:i + BATCH_SIZE] for i in range(0, len(vehicles), BATCH_SIZE)]
        for i, batch in enumerate(batches):
            is_last = (i == len(batches) - 1)
            send_batch(
                vehicles=batch,
                event_id=event_id,
                active_refs=active_refs if is_last else [],
                cleanup=is_last,
            )
            time.sleep(3)

    # ==========================================
    # CLEANUP DES VENTES TERMINÉES
    # ==========================================
    if terminated_events:
        print(f"\n🗑️ Nettoyage {len(terminated_events)} ventes terminées...")
        send_batch(
            vehicles=[],
            event_id="",
            active_refs=[],
            cleanup=False,
            terminated_events=terminated_events,
        )

    print(f"\n🎯 TERMINÉ")


if __name__ == "__main__":
    asyncio.run(main())
