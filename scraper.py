import asyncio
import json
import os
import re
import sys
from playwright.async_api import async_playwright

ALCOPA_URLS = [
    "https://www.alcopa-auction.fr/salle-de-vente-encheres/nancy/2110",
]


async def diagnostic(url: str):
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

        print(f"=== Chargement : {url} ===")
        await page.goto(url, wait_until="networkidle", timeout=60000)

        # Attendre encore un peu
        await asyncio.sleep(3)

        html = await page.content()
        await browser.close()

        print(f"\n=== Taille du HTML : {len(html)} caractères ===\n")

        # 1. Chercher toutes les variables "window.*" qui contiennent "search" ou "vehicle" ou "car"
        print("=== Variables window.* détectées ===")
        matches = re.findall(r'window\.([A-Za-z0-9_.]+)\s*=\s*', html)
        unique_vars = sorted(set(matches))
        for v in unique_vars[:80]:  # limite à 80
            print(f"  window.{v}")

        # 2. Chercher toutes les balises <script> qui contiennent des données JSON massives
        print("\n=== Scripts contenant des données volumineuses (>5000 caractères) ===")
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
        for i, s in enumerate(scripts):
            if len(s) > 5000:
                # Cherche des indices
                preview = s[:300].replace('\n', ' ').replace('\r', '')
                print(f"\n  [Script {i}] taille={len(s)}")
                print(f"    Début: {preview}")

        # 3. Chercher "JSON.parse" ou des objets JSON massifs
        print("\n=== Occurrences de 'JSON.parse' ===")
        jsonparse = re.findall(r'JSON\.parse\((.{0,200}?)\)', html)
        for i, m in enumerate(jsonparse[:10]):
            print(f"  [{i}] {m[:150]}")

        # 4. Chercher des clés spécifiques véhicule
        print("\n=== Recherche de mots-clés ===")
        for keyword in ["detailed_model", "first_registration", "mileage", "Alcopa", "searchResults", "offers", "vehicles"]:
            count = html.count(keyword)
            print(f"  '{keyword}' : {count} occurrences")

        # 5. Chercher des appels API dans le HTML
        print("\n=== URLs d'API détectées dans le HTML ===")
        api_urls = re.findall(r'["\'](/api/[^"\']+|https?://[^"\']*api[^"\']*)["\']', html)
        for u in sorted(set(api_urls))[:20]:
            print(f"  {u}")


async def main():
    for url in ALCOPA_URLS:
        await diagnostic(url)


if __name__ == "__main__":
    asyncio.run(main())
