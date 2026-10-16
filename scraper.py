import json
import os
import re
import sys
import time
import requests
import undetected_chromedriver as uc
from bs4 import BeautifulSoup

# ============================================
# CONFIGURATION
# ============================================
WP_URL = os.environ["WP_URL"]
WP_KEY = os.environ["WP_API_KEY"]

ALCOPA_HOME = "https://www.alcopa-auction.fr/"
MAX_SALES_PER_RUN = 5  # Réduit pour tester plus vite


def get_driver():
    """Crée un Chrome non-détectable via undetected-chromedriver."""
    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    # ⚠️ PAS de --headless : on utilise xvfb (écran virtuel) à la place

    driver = uc.Chrome(options=options, version_main=141)
    return driver


def fetch_page(driver, url: str) -> str:
    """Charge une page et attend le contenu."""
    print(f"📥 Fetch : {url}")
    try:
        driver.get(url)
        # Attend que le DOM soit prêt
        time.sleep(6)  # Le temps que le WAF résolve son challenge JS
        html = driver.page_source
        print(f"   ✅ {len(html)} caractères")
        return html
    except Exception as e:
        print(f"   ❌ Erreur : {e}")
        return ""


def extract_sale_urls(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    sale_urls = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/salle-de-vente-encheres/" in href:
            if href.startswith("/"):
                href = "https://www.alcopa-auction.fr" + href
            clean = href.split("?")[0].split("#")[0]
            sale_urls.add(clean)
    return sorted(sale_urls)


def extract_vehicles(html: str) -> list:
    if not html:
        return []

    # Variable JSON Alcopa
    match = re.search(
        r"window\.Alcopa\.searchResultsJSONString\s*=\s*'(.*?)';",
        html,
        re.DOTALL,
    )
    if match:
        try:
            raw = match.group(1)
            data = json.loads(json.loads(f'"{raw}"'))
            vehicles = data.get("car", [])
            if vehicles:
                print(f"   ✅ {len(vehicles)} véhicules (JSON)")
                return vehicles
        except Exception as e:
            print(f"   ⚠️ JSON parsing : {e}")

    print("   ⚠️ Aucun véhicule")
    return []


def main():
    driver = get_driver()
    try:
        # 1. Home
        home_html = fetch_page(driver, ALCOPA_HOME)
        if not home_html:
            print("❌ Home inaccessible")
            sys.exit(1)

        # Debug : sauvegarde HTML
        with open("debug_home.html", "w", encoding="utf-8") as f:
            f.write(home_html)

        # Diagnostic : est-ce le vrai contenu ?
        if len(home_html) < 50000:
            print(f"⚠️ HTML trop petit ({len(home_html)}) — WAF probable")
            print("   Extrait du HTML :")
            print(home_html[:1500])
            sys.exit(1)

        sale_urls = extract_sale_urls(home_html)
        print(f"\n🎯 {len(sale_urls)} ventes trouvées")

        if not sale_urls:
            print("⚠️ Aucune URL de vente")
            sys.exit(0)

        all_vehicles = []
        for i, sale_url in enumerate(sale_urls[:MAX_SALES_PER_RUN], 1):
            print(f"\n--- [{i}/{min(len(sale_urls), MAX_SALES_PER_RUN)}] {sale_url} ---")
            html = fetch_page(driver, sale_url)
            if not html:
                continue
            vehicles = extract_vehicles(html)
            for v in vehicles:
                v["_sale_url"] = sale_url
            all_vehicles.extend(vehicles)
            time.sleep(3)

        print(f"\n📦 Total : {len(all_vehicles)} véhicules")

        if not all_vehicles:
            print("⚠️ Aucun véhicule, arrêt.")
            sys.exit(0)

        # 2. Envoi WordPress
        payload = {
            "source": "Alcopa Auction",
            "total": len(all_vehicles),
            "vehicules": all_vehicles,
        }

        print("📤 Envoi à WordPress...")
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
            print(f"❌ Erreur WordPress ({response.status_code}) : {response.text}")
            sys.exit(1)

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
