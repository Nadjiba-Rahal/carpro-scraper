import asyncio
import json
import os
import re
import sys
import requests
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

VPAUTO_URL = "https://www.vpauto.fr/vehicule/liste?event=43301"


async def diagnostic():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            locale="fr-FR", timezone_id="Europe/Paris",
        )
        page = await context.new_page()
        stealth = Stealth()
        await stealth.apply_stealth_async(page)

        print(f"📥 Chargement : {VPAUTO_URL}")
        await page.goto(VPAUTO_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(8)

        html = await page.content()
        await browser.close()

        print(f"📏 Taille : {len(html)} caractères\n")

        # 1. Cherche les liens vers les détails de véhicules
        print("=== LIENS VÉHICULES ===")
        links = re.findall(r'href="(/[^"]*vehicule[^"]*)"', html)
        unique_links = sorted(set(links))
        print(f"Trouvé {len(unique_links)} liens uniques :")
        for l in unique_links[:10]:
            print(f"  {l}")

        # 2. Cherche la structure autour d'une "Mise à prix"
        print("\n=== CONTEXTE AUTOUR D'UNE 'MISE À PRIX' ===")
        idx = html.find("Mise à prix")
        if idx > 0:
            start = max(0, idx - 2500)
            end = min(len(html), idx + 1000)
            extract = html[start:end]
            print(f"Extrait ({len(extract)} car.) :\n")
            print(extract)
            with open("debug_extract.html", "w", encoding="utf-8") as f:
                f.write(extract)

        # 3. Cherche les balises <article>
        print("\n\n=== BALISES ARTICLE/CARD ===")
        articles = re.findall(r'<(article|div)[^>]*class="([^"]*(?:card|vehicule|vehicle|lot|annonce)[^"]*)"', html, re.IGNORECASE)
        for tag, cls in articles[:10]:
            print(f"  <{tag} class=\"{cls}\">")

        # 4. Cherche les images de véhicules
        print("\n=== IMAGES VÉHICULES ===")
        imgs = re.findall(r'<img[^>]*src="(https?://[^"]*)"[^>]*>', html)
        vp_imgs = [i for i in imgs if 'vpauto' in i or 'cloudinary' in i or 'vehicle' in i.lower()]
        print(f"Trouvé {len(vp_imgs)} images :")
        for i in vp_imgs[:5]:
            print(f"  {i}")

        # 5. Cherche des appels API dans le code source
        print("\n=== APPELS API POSSIBLES ===")
        api_matches = re.findall(r'["\'](/api/[^"\']+|https?://[^"\']*api[^"\']*)["\']', html)
        for api in sorted(set(api_matches))[:15]:
            print(f"  {api}")

        # 6. Cherche les balises <a> avec des liens de véhicules
        print("\n=== EXEMPLES DE LIENS DÉTAIL ===")
        detail_links = re.findall(r'<a[^>]*href="([^"]*)"[^>]*>([^<]{0,80})</a>', html)
        for href, text in detail_links[:20]:
            if 'vehicule' in href.lower() or 'lot' in href.lower() or 'detail' in href.lower():
                print(f"  {href}  →  {text.strip()}")


if __name__ == "__main__":
    asyncio.run(diagnostic())
