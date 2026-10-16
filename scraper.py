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

# Plusieurs URLs à tester
TEST_URLS = [
    "https://www.alcopa-auction.fr/calendrier-des-ventes",
    "https://www.alcopa-auction.fr/",
    "https://www.alcopa-auction.fr/salle-de-vente-encheres/nancy/2110",
]


async def test_url(url: str):
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

        print(f"\n{'='*60}")
        print(f"URL : {url}")
        print(f"{'='*60}")

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(8)
            html = await page.content()
            print(f"📏 Taille HTML : {len(html)} caractères")

            # Cherche des indices de contenu réel
            if len(html) > 50000:
                print("✅ CONTENU RÉEL CHARGÉ")

                # Essaie d'extraire les liens de ventes
                links = re.findall(r'href="(/salle-de-vente-encheres/[^"]+)"', html)
                unique_links = sorted(set(links))
                print(f"🔗 {len(unique_links)} liens de ventes trouvés :")
                for l in unique_links[:15]:
                    print(f"   https://www.alcopa-auction.fr{l}")

                # Cherche la variable JSON
                if "searchResultsJSONString" in html:
                    print("✅ Variable JSON présente")
                else:
                    print("❌ Variable JSON absente")
            else:
                print("❌ CONTENU BLOQUÉ PAR WAF")
                # Cherche un indice du WAF
                if "awsWaf" in html or "gokuProps" in html:
                    print("🛡️ AWS WAF détecté")

        except Exception as e:
            print(f"❌ Erreur : {e}")

        await browser.close()


async def main():
    for url in TEST_URLS:
        await test_url(url)
        await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
