"""Кадры панели → webp для лендинга.

Берёт готовые скриншоты из docs/img (их делает docs/make-shots.py) и кладёт в
landing/site/shots двумя размерами: обычным для страницы и @2x для лайтбокса.
Исходники сняты в двойном разрешении, поэтому «обычный» — ровно половина.
"""
import os

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "docs", "img")
DST = os.path.join(ROOT, "landing", "site", "shots")
SHOTS = ("home", "servers", "server-detail", "sites", "services", "backups")

for lang, sub in (("ru", "ru"), ("en", "")):
    out = os.path.join(DST, lang)
    os.makedirs(out, exist_ok=True)
    for name in SHOTS:
        src = os.path.join(SRC, sub, name + ".png") if sub else os.path.join(SRC, name + ".png")
        if not os.path.exists(src):
            print("нет исходника:", src)
            continue
        im = Image.open(src).convert("RGB")
        im.save(os.path.join(out, name + "@2x.webp"), "WEBP", quality=82, method=6)
        half = im.resize((im.width // 2, im.height // 2), Image.LANCZOS)
        half.save(os.path.join(out, name + ".webp"), "WEBP", quality=84, method=6)
        kb = os.path.getsize(os.path.join(out, name + ".webp")) // 1024
        print(f"{lang}/{name}: {half.width}x{half.height}, {kb} KB")
