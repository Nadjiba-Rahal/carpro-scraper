import json
import os
import re
import sys
import time
from curl_cffi import requests
from bs4 import BeautifulSoup

# ============================================
# CONFIGURATION
# ============================================
WP_URL = os.environ["WP_URL"]
WP_KEY = os.environ["WP_API_KEY"]

ALCOPA_HOME = "https://www.alcopa-auction.fr/"
MAX_SALES_PER_RUN = 10


def fetch(url: str) -> str:
    """Récupère le HTML avec curl_cffi (fingerprint Chrome)."""
    print(f"📥 Fetch : {url}")
    try:
        # impersonate="chrome" → imite le vrai Chrome (TLS + headers)
        response = requests.get(
            url,
            impersonate="chrome",
            timeout=60,
            allow_redirects=True,
        )
    except Exception as e:
        print(f"   ❌ Erreur : {e}")
        return ""

    print(f"   HTTP {response.status_code} | {len(response.text)} caractères")

    if response.status_code == 200:
        return response.text
    return ""


def extract_sale_urls(html: str) -> list:
    """Extrait les URLs des ventes actives."""
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
    """Extrait les véhicules (JSON Alcopa)."""
    if not html:
        return []

    # Méthode 1 : variable JSON officielle
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

    # Méthode 2 : scripts embarqués
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
    # 1. Récupère la home
    home_html = fetch(ALCOPA_HOME)
    if not home_html:
        print("❌ Home inaccessible")
        sys.exit(1)

    # Debug : sauvegarde le HTML pour analyse
    with open("debug_home.html", "w", encoding="utf-8") as f:
        f.write(home_html)

    sale_urls = extract_sale_urls(home_html)
    print(f"\n🎯 {len(sale_urls)} ventes trouvées")

    if not sale_urls:
        print("⚠️ Aucune URL de vente — voir debug_home.html")
        sys.exit(0)

    all_vehicles = []
    for i, sale_url in enumerate(sale_urls[:MAX_SALES_PER_RUN], 1):
        print(f"\n--- [{i}/{min(len(sale_urls), MAX_SALES_PER_RUN)}] {sale_url} ---")
        html = fetch(sale_url)
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
        impersonate="chrome",
    )

    if response.status_code == 200:
        print(f"✅ WordPress : {response.json()}")
    else:
        print(f"❌ Erreur WordPress ({response.status_code}) : {response.text}")
        sys.exit(1)


if __name__ == "__main__":
    main()
