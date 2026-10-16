import json
import os
import re
import sys
import time
import requests
from bs4 import BeautifulSoup

# ============================================
# CONFIGURATION
# ============================================
WP_URL = os.environ["WP_URL"]
WP_KEY = os.environ["WP_API_KEY"]
SCRAPINGANT_KEY = os.environ["SCRAPINGANT_KEY"]

ALCOPA_HOME = "https://www.alcopa-auction.fr/"

# Nombre max de ventes à traiter par run (pour économiser le quota)
MAX_SALES_PER_RUN = 10


def fetch_via_scrapingant(url: str) -> str:
    """Récupère le HTML via ScrapingAnt (contourne AWS WAF)."""
    print(f"📥 Fetch : {url}")

    try:
        response = requests.get(
            "https://api.scrapingant.com/v2/general",
            params={
                "url": url,
                "x-api-key": SCRAPINGANT_KEY,
                "browser": "true",
                "proxy_country": "fr",
                "proxy_type": "residential",  # Utilisation de proxys résidentiels
                "wait_for_selector": ".js-search-results-json", # À adapter, voir ci-dessous
            },
            timeout=120, # Augmenté pour laisser le temps au WAF
        )
    except Exception as e:
        print(f"   ❌ Erreur réseau : {e}")
        return ""

    if response.status_code == 200:
        html = response.text
        print(f"   ✅ {len(html)} caractères reçus")
        return html
    else:
        print(f"   ❌ HTTP {response.status_code} : {response.text[:200]}")
        return ""

def extract_sale_urls(html: str) -> list:
    """Extrait les URLs des ventes actives depuis la page d'accueil."""
    soup = BeautifulSoup(html, "html.parser")
    sale_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/salle-de-vente-encheres/" in href:
            if href.startswith("/"):
                href = "https://www.alcopa-auction.fr" + href
            # Évite les doublons en retirant les query params
            clean = href.split("?")[0].split("#")[0]
            sale_urls.add(clean)

    return sorted(sale_urls)


def extract_vehicles_from_sale(html: str) -> list:
    """Extrait les véhicules depuis une page de vente Alcopa."""
    if not html:
        return []

    # Méthode 1 : variable JSON Alcopa
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
            print(f"   ⚠️ Erreur parsing JSON : {e}")

    # Méthode 2 : cherche un JSON embarqué dans une balise script
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    for s in scripts:
        if '"car"' in s and '"brand"' in s and len(s) > 5000:
            try:
                # Trouve le premier { ... dernier }
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

    print("   ⚠️ Aucun véhicule trouvé sur cette page")
    return []


def main():
    all_vehicles = []

    # 1. Page d'accueil pour trouver les ventes actives
    home_html = fetch_via_scrapingant(ALCOPA_HOME)
    if not home_html:
        print("❌ Impossible de charger la home Alcopa")
        sys.exit(1)

    sale_urls = extract_sale_urls(home_html)
    print(f"\n🎯 {len(sale_urls)} ventes trouvées")

    if not sale_urls:
        print("⚠️ Aucune vente dans le HTML — structure à vérifier")
        # Debug : sauvegarde le HTML
        with open("debug_home.html", "w", encoding="utf-8") as f:
            f.write(home_html)
        sys.exit(0)

    # Limite pour économiser le quota
    sales_to_process = sale_urls[:MAX_SALES_PER_RUN]
    print(f"📋 Traitement de {len(sales_to_process)} ventes\n")

    for i, sale_url in enumerate(sales_to_process, 1):
        print(f"--- [{i}/{len(sales_to_process)}] {sale_url} ---")
        sale_html = fetch_via_scrapingant(sale_url)
        if not sale_html:
            continue

        vehicles = extract_vehicles_from_sale(sale_html)
        for v in vehicles:
            v["_sale_url"] = sale_url
        all_vehicles.extend(vehicles)

        # Petit délai pour ne pas spammer
        time.sleep(1)

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
    try:
        response = requests.post(
            WP_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {WP_KEY}",
                "Content-Type": "application/json",
            },
            timeout=120,
        )
    except Exception as e:
        print(f"❌ Erreur réseau WordPress : {e}")
        sys.exit(1)

    if response.status_code == 200:
        print(f"✅ WordPress : {response.json()}")
    else:
        print(f"❌ Erreur WordPress ({response.status_code}) : {response.text}")
        sys.exit(1)


if __name__ == "__main__":
    main()
