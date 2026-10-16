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

# URL de la liste des ventes actives VPauto
VPAUTO_URL = "https://www.vpauto.fr/vehicule/liste?event=43301"


async def diagnostic():
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

        print(f"📥 Chargement : {VPAUTO_URL}")
        await page.goto(VPAUTO_URL, wait_until="domcontentloaded", timeout=60000)

        # Attend que le contenu dynamique se charge
        await asyncio.sleep(10)

        html = await page.content()
        await browser.close()

        print(f"📏 Taille HTML : {len(html)} caractères")

        # Sauvegarde pour analyse
        with open("debug_vpauto.html", "w", encoding="utf-8") as f:
            f.write(html)

        # Cherche des indices de contenu
        print("\n=== Recherche de mots-clés ===")
        for keyword in ["mise à prix", "Mise à prix", "TAVASCAN", "CUPRA", "vehicule", "card", "annonce", "lot"]:
            count = html.lower().count(keyword.lower())
            if count > 0:
                print(f"  ✅ '{keyword}' : {count} occurrences")

        # Cherche les variables JSON ou objets JavaScript
        print("\n=== Variables window.* détectées ===")
        matches = re.findall(r'window\.([A-Za-z0-9_.]+)\s*=', html)
        for v in sorted(set(matches))[:40]:
            print(f"  window.{v}")

        # Cherche des scripts contenant des données
        print("\n=== Scripts volumineux ===")
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
        for i, s in enumerate(scripts):
            if len(s) > 3000:
                print(f"  [Script {i}] taille={len(s)}")
                print(f"    Début: {s[:200].strip()}")

        # Cherche la structure des cartes véhicules
        print("\n=== Structure HTML des véhicules ===")
        # Essaie différents sélecteurs
        for selector in [
            "article", "[class*='vehicule']", "[class*='card']",
            "[class*='annonce']", "[class*='lot']", "[class*='vehicle']",
            "li[class*='item']", "div[class*='item']"
        ]:
            try:
                elements = await page.query_selector_all(selector) if False else []
            except:
                elements = []
        # Note : page est fermé, on cherche dans le HTML
        for sel in ["article", "vehicule", "card", "annonce", "lot"]:
            count = html.count(f'class="{sel}') + html.count(f"class='{sel}")
            if count > 0:
                print(f"  class*='{sel}' : ~{count} occurrences")

        print("\n✅ Diagnostic terminé. Fichier debug_vpauto.html sauvegardé.")


if __name__ == "__main__":
    asyncio.run(diagnostic())
