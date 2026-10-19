"""Кадры панели → webp для лендинга.

Берёт готовые скриншоты из docs/img (их делает docs/make-shots.py) и кладёт в
landing/site/shots двумя размерами: обычным для страницы и @2x для лайтбокса.
Исходники сняты в двойном разрешении, поэтому «обычный» — ровно половина.
Заодно ставит в HTML лендинга настоящие width/height у этих картинок: высота
кадра зависит от содержимого, а без верных размеров страница прыгает при загрузке.
"""
import os
import re

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "docs", "img")
DST = os.path.join(ROOT, "landing", "site", "shots")
SITE = os.path.join(ROOT, "landing", "site")
SHOTS = ("home", "server", "disks", "disk-health", "security-ports", "security-access", "web", "gpu", "llm",
         "vulns", "alerts", "services", "services-app", "kube", "sites", "backups")
PAGES = {"ru": os.path.join(SITE, "index.html"), "en": os.path.join(SITE, "en", "index.html")}

sizes: dict[tuple[str, str], tuple[int, int]] = {}
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
        sizes[(lang, name)] = (half.width, half.height)
        kb = os.path.getsize(os.path.join(out, name + ".webp")) // 1024
        print(f"{lang}/{name}: {half.width}x{half.height}, {kb} KB")

IMG = re.compile(r'<img src="/shots/(ru|en)/([a-z0-9-]+)\.webp"[^>]*>')


def fix(m: re.Match) -> str:
    tag, wh = m.group(0), sizes.get((m.group(1), m.group(2)))
    if not wh:
        return tag
    tag = re.sub(r'\swidth="\d+"', f' width="{wh[0]}"', tag)
    return re.sub(r'\sheight="\d+"', f' height="{wh[1]}"', tag)


for lang, page in PAGES.items():
    with open(page, encoding="utf-8") as f:
        html = f.read()
    new = IMG.sub(fix, html)
    if new != html:
        with open(page, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
        print("размеры кадров обновлены:", os.path.relpath(page, ROOT))
