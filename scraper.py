import asyncio
import json
import os
import re
import sys
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

WP_URL = os.environ["WP_URL"]
WP_KEY = os.environ["WP_API_KEY"]

# URL de la liste des véhicules VPauto (event 43301 = dernière vente)
VPAUTO_URL = "https://www.vpauto.fr/vehicule/liste?event=43301"


async def fetch_html(url: str) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="fr-FR",
            timezone_id="Europe/Paris",
        )
        page = await context.new_page()
        stealth = Stealth()
        await stealth.apply_stealth_async(page)

        print(f"📥 Chargement : {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(8)

        html = await page.content()
        await browser.close()
        print(f"📏 {len(html)} caractères reçus")
        return html


def clean_int(text: str):
    """Extrait un entier d'une chaîne (ex: '26 800€' → 26800)."""
    if not text:
        return 0
    n = re.sub(r"[^\d]", "", text)
    return int(n) if n else 0


def parse_vehicles(html: str) -> list:
    """Parse tous les véhicules depuis la page de liste."""
    soup = BeautifulSoup(html, "html.parser")
    vehicles = []

    # Chaque véhicule = <article class="element">
    articles = soup.find_all("article", class_="element")
    print(f"🔍 {len(articles)} articles trouvés")

    for art in articles:
        try:
            v = {}

            # 1. ID Alcopa interne + référence
            v["_etincelle_id"] = art.get("data-vehicle-etincelle-id", "")

            # 2. Lien vers la fiche
            link = art.find("a", href=True)
            if not link:
                continue
            href = link["href"]
            v["_url"] = "https://www.vpauto.fr" + href if href.startswith("/") else href

            # Référence CarPro = partie après /vehicule/
            ref_match = re.search(r"/vehicule/([^/]+)/", href)
            v["id"] = ref_match.group(1) if ref_match else v["_etincelle_id"]

            # 3. Image
            img = art.find("img")
            v["images"] = [img["src"]] if img and img.get("src") else []

            # 4. Marque
            marque_div = art.find("div", class_=re.compile(r"elmt-marque"))
            v["brand"] = marque_div.find("h2").get_text(strip=True) if marque_div and marque_div.find("h2") else ""

            # 5. Modèle
            modele_div = art.find("div", class_="elmt-modele")
            if modele_div:
                h3 = modele_div.find("h3")
                v["detailed_model"] = h3.get_text(strip=True) if h3 else ""
                # Année + Km
                infos = modele_div.find_all("span")
                if len(infos) >= 2:
                    v["year"] = clean_int(infos[0].get_text())
                    v["mileage"] = clean_int(infos[1].get_text())
            else:
                v["detailed_model"] = ""

            # 6. Prix
            prix_div = art.find("div", class_="elmt-prix")
            if prix_div:
                price_span = prix_div.find("span", class_="prix")
                v["price"] = clean_int(price_span.get_text()) if price_span else 0

                # Détermine si c'est "Mise à prix" ou "Adjugé"
                label = prix_div.find("span")
                v["_price_label"] = label.get_text(strip=True) if label else ""
            else:
                v["price"] = 0
                v["_price_label"] = ""

            # 7. État de vente (Adjugé / En cours / etc.)
            saling = art.find("div", class_="vehicle-salingState")
            if saling:
                spans = saling.find_all("span")
                v["_sale_state"] = spans[0].get_text(strip=True) if spans else ""
                v["_sale_final_price"] = clean_int(spans[1].get_text()) if len(spans) > 1 else 0

            # 8. Localisation (département)
            ville = art.find("span", class_="elmt-ville")
            v["_dept"] = ville.get_text(strip=True) if ville else ""

            # 9. Numéro d'ordre dans la vente
            ordre = art.find("span", class_="elmt-ordre")
            v["_ordre"] = ordre.get_text(strip=True) if ordre else ""

            # 10. Valeurs par défaut CarPro
            v["fuel"] = ""
            v["gearbox"] = ""
            v["first_registration"] = str(v.get("year", ""))

            vehicles.append(v)
        except Exception as e:
            print(f"⚠️ Erreur parsing un article : {e}")
            continue

    return vehicles


async def main():
    html = await fetch_html(VPAUTO_URL)

    if not html or len(html) < 50000:
        print("❌ HTML trop petit — site inaccessible")
        sys.exit(1)

    # Debug : sauvegarde le HTML
    with open("debug_vpauto.html", "w", encoding="utf-8") as f:
        f.write(html)

    vehicles = parse_vehicles(html)
    print(f"\n📦 {len(vehicles)} véhicules extraits")

    if not vehicles:
        print("⚠️ Aucun véhicule, arrêt.")
        sys.exit(0)

    # Affiche un aperçu
    print("\n--- Aperçu des 3 premiers ---")
    for v in vehicles[:3]:
        print(f"  {v.get('brand', '?'):<12} | {v.get('detailed_model', '?')[:40]:<40} | {v.get('price', 0):>7} € | {v.get('_dept', '?')}")

    # Envoi à WordPress
    payload = {
        "source": "VPauto",
        "total": len(vehicles),
        "vehicules": vehicles,
    }

    print(f"\n📤 Envoi à WordPress ({len(vehicles)} véhicules)...")
    response = requests.post(
        WP_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {WP_KEY}",
            "Content-Type": "application/json",
        },
        timeout=120,
    )

    if response.status_code == 200:
        print(f"✅ WordPress : {response.json()}")
    else:
        print(f"❌ Erreur WordPress ({response.status_code}) : {response.text[:500]}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
