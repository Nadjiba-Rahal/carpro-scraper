import json
import os
import re
import sys
import time
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ============================================
# CONFIGURATION
# ============================================
WP_URL = os.environ["WP_URL"]
WP_KEY = os.environ["WP_API_KEY"]

ALCOPA_HOME = "https://www.alcopa-auction.fr/"
MAX_SALES_PER_RUN = 10


def get_driver():
    """Crée un navigateur Chrome headless avec des options furtives."""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    # Désactive les indicateurs d'automatisation
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(options=options)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


def fetch_page(driver, url: str) -> str:
    """Charge une page avec Selenium et retourne le HTML."""
    print(f"📥 Fetch : {url}")
    try:
        driver.get(url)
        # Attend que le contenu soit chargé (max 30s)
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        time.sleep(3)  # Laisse le temps au JS de s'exécuter
        html = driver.page_source
        print(f"   ✅ {len(html)} caractères reçus")
        return html
    except Exception as e:
        print(f"   ❌ Erreur : {e}")
        return ""


def extract_sale_urls(html: str) -> list:
    """Extrait les URLs des ventes actives depuis la page d'accueil."""
    from bs4 import BeautifulSoup
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
    """Extrait les véhicules depuis la page (JSON Alcopa)."""
    if not html:
        return []

    # Variable JSON officielle d'Alcopa
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
                print(f"   ✅ {len(vehicles)} véhicules (JSON direct)")
                return vehicles
        except Exception as e:
            print(f"   ⚠️ Parsing JSON : {e}")

    # Fallback : cherche un script contenant les données
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    for s in scripts:
        if '"car"' in s and '"brand"' in s and len(s) > 5000:
            try:
                start = s.find("{")
                end = s.rfind("}")
                if start >= 0 and end > start:
                    data = json.loads(s[start : end + 1])
                    vehicles = data.get("car", [])
                    if vehicles:
                        print(f"   ✅ {len(vehicles)} véhicules (script embarqué)")
                        return vehicles
            except Exception:
                continue

    print("   ⚠️ Aucun véhicule trouvé")
    return []


def main():
    driver = get_driver()

    try:
        # 1. Page d'accueil pour trouver les ventes
        home_html = fetch_page(driver, ALCOPA_HOME)
        if not home_html:
            print("❌ Home inaccessible")
            sys.exit(1)

        # Debug
        with open("debug_home.html", "w", encoding="utf-8") as f:
            f.write(home_html)

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

            time.sleep(2)

        print(f"\n📦 Total : {len(all_vehicles)} véhicules")

        if not all_vehicles:
            print("⚠️ Aucun véhicule, arrêt.")
            sys.exit(0)

        # 2. Envoi à WordPress
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
