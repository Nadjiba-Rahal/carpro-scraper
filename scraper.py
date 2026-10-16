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


async def scrape_alcopa(url: str) -> list[dict]:
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

        # Attendre que le contenu se charge
        await asyncio.sleep(8)

        html = await page.content()
        await browser.close()

        print(f"HTML récupéré : {len(html)} caractères")

        match = re.search(
            r"window\.Alcopa\.searchResultsJSONString\s*=\s*'(.*?)';",
            html,
            re.DOTALL
        )
        if not match:
            print("JSON non trouvé")
            # Sauvegarde le HTML pour debug
            with open("debug.html", "w", encoding="utf-8") as f:
                f.write(html)
            return []

        data = json.loads(json.loads(f'"{match.group(1)}"'))
        return data.get("car", [])


async def main():
    for url in ALCOPA_URLS:
        await diagnostic(url)


if __name__ == "__main__":
    asyncio.run(main())
