import asyncio
import json
import os
import re
import sys
import requests
from playwright.async_api import async_playwright

# ============================================
# CONFIGURATION via variables d'environnement
# (stockées dans GitHub Secrets, pas dans le code)
# ============================================
WP_URL = os.environ["WP_URL"]           # ex: https://carpro-services.com/wp-json/carpro/v1/import
WP_KEY = os.environ["WP_API_KEY"]       # clé secrète partagée

ALCOPA_URLS = [
    "https://www.alcopa-auction.fr/salle-de-vente-encheres/nancy/2110",
    # Ajoute d'autres ventes ici
]


async def scrape_alcopa(url: str) -> list[dict]:
    """Scrappe une page de vente Alcopa et retourne les véhicules."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_extra_http_headers({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        })

        print(f"🌐 Chargement : {url}")
        await page.goto(url, wait_until="networkidle", timeout=60000)
        html = await page.content()
        await browser.close()

        match = re.search(
            r"window\.Alcopa\.searchResultsJSONString\s*=\s*'(.*?)';",
            html,
            re.DOTALL
        )
        if not match:
            print("❌ JSON non trouvé")
            return []

        data = json.loads(json.loads(f'"{match.group(1)}"'))
        return data.get("car", [])


async def main():
    all_vehicles = []

    for url in ALCOPA_URLS:
        try:
            vehicles = await scrape_alcopa(url)
            all_vehicles.extend(vehicles)
            print(f"✅ {len(vehicles)} véhicules extraits de {url}")
        except Exception as e:
            print(f"❌ Erreur sur {url}: {e}")
        await asyncio.sleep(2)

    print(f"📦 Total : {len(all_vehicles)} véhicules")

    if not all_vehicles:
        print("⚠️ Aucun véhicule, arrêt.")
        sys.exit(0)

    # Envoi à WordPress
    payload = {
        "source": "Alcopa Auction",
        "total": len(all_vehicles),
        "vehicules": all_vehicles,
    }

    print(f"📤 Envoi à WordPress...")
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


if __name__ == "__main__":
    asyncio.run(main())