import asyncio
import json
import os
import re
import sys
import requests
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

WP_URL = os.environ["WP_URL"]
WP_KEY = os.environ["WP_API_KEY"]

ALCOPA_URLS = [
    "https://www.alcopa-auction.fr/salle-de-vente-encheres/nancy/2110",
]


async def scrape_alcopa(url: str) -> list:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
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

        print(f"Chargement : {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        await asyncio.sleep(10)

        html = await page.content()
        await browser.close()

        print(f"HTML recupere : {len(html)} caracteres")

        match = re.search(
            r"window\.Alcopa\.searchResultsJSONString\s*=\s*'(.*?)';",
            html,
            re.DOTALL
        )

        if not match:
            print("JSON non trouve")
            return []

        data = json.loads(json.loads(f'"{match.group(1)}"'))
        return data.get("car", [])


async def main():
    all_vehicles = []

    for url in ALCOPA_URLS:
        try:
            vehicles = await scrape_alcopa(url)
            all_vehicles.extend(vehicles)
            print(f"{len(vehicles)} vehicules extraits de {url}")
        except Exception as e:
            print(f"Erreur sur {url}: {e}")
        await asyncio.sleep(2)

    print(f"Total : {len(all_vehicles)} vehicules")

    if not all_vehicles:
        print("Aucun vehicule, arret.")
        sys.exit(0)

    payload = {
        "source": "Alcopa Auction",
        "total": len(all_vehicles),
        "vehicules": all_vehicles,
    }

    print("Envoi a WordPress...")
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
        print(f"WordPress : {response.json()}")
    else:
        print(f"Erreur WordPress ({response.status_code}) : {response.text}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
