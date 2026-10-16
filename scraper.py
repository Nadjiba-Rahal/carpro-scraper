import asyncio
import re
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

# Une page détail de véhicule (le CUPRA de tout à l'heure)
DETAIL_URL = "https://www.vpauto.fr/vehicule/VS00019MV2601221/cupra-tavascan-286ch-v"


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

        print(f"📥 {DETAIL_URL}")
        await page.goto(DETAIL_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(8)

        html = await page.content()
        await browser.close()

        print(f"📏 {len(html)} caractères\n")

        # Sauvegarde
        with open("debug_detail.html", "w", encoding="utf-8") as f:
            f.write(html)

        # Cherche les infos clés
        print("=== RECHERCHE DE CHAMPS ===")
        keywords = [
            "Énergie", "Energie", "Carburant", "Diesel", "Essence", "Électrique",
            "Boîte", "Boite", "Automatique", "Manuelle",
            "Mise en circulation", "1ère mise",
            "VIN", "Châssis", "Chassis",
            "Puissance", "ch", "kW",
            "km", "Kilométrage",
        ]
        for kw in keywords:
            count = html.count(kw)
            if count > 0:
                print(f"  '{kw}' : {count} occurrences")

        # Cherche les blocs de données structurées
        print("\n=== BLOCS DE DONNÉES ===")
        patterns = [
            (r'<div[^>]*class="[^"]*fiche[^"]*"', "fiche"),
            (r'<div[^>]*class="[^"]*carac[^"]*"', "carac"),
            (r'<div[^>]*class="[^"]*spec[^"]*"', "spec"),
            (r'<div[^>]*class="[^"]*detail[^"]*"', "detail"),
            (r'<div[^>]*class="[^"]*info[^"]*"', "info"),
            (r'<table[^>]*>', "table"),
        ]
        for pat, label in patterns:
            matches = re.findall(pat, html, re.IGNORECASE)
            if matches:
                print(f"  {label} : {len(matches)} blocs")

        # Cherche les images
        print("\n=== IMAGES ===")
        imgs = re.findall(r'(?:src|data-src)="(https?://[^"]*\.(?:jpg|jpeg|png|webp)[^"]*)"', html)
        vp_imgs = [i for i in imgs if 'vpauto' in i or 'cloudinary' in i]
        print(f"Trouvé {len(vp_imgs)} images VPauto :")
        for img in vp_imgs[:10]:
            print(f"  {img}")

        # Cherche un JSON structuré
        print("\n=== JSON EMBARQUÉ ===")
        json_scripts = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL)
        for i, js in enumerate(json_scripts):
            print(f"\n--- JSON-LD {i} ---")
            print(js[:800])


if __name__ == "__main__":
    asyncio.run(diagnostic())
