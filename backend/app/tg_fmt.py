"""Короткий формат алерта в Telegram (формат A, 21.10.2026).

Приходили простыни: "больше всего памяти", "за 7 дней OOM 13 раз", пороги, лимиты, хвосты
логов, команды. Человеку в чате нужно три вещи: где, что случилось и что именно сломалось.
Остальное - в панели, куда ведет ссылка с имени сервера:

    🔴🧠 k8s-a
    Кончилась память (OOM)
    Убит: mariadbd
    Под: wordpress-mariadb (лимит 1гб)

Первая строка - эмодзи уровня и темы и имя ссылкой, вторая - что случилось одной фразой,
дальше одна-две строки конкретики: процесс, под без хеша, контейнер, юнит, репозиторий.
Отбой - одна строка "✅ сервер: что снова в норме". В пачке каждый алерт одной строкой.

Символы только с клавиатуры: в чате без длинных тире, "≥", "↳", кавычек-елочек, "·", "…" и ё.
Исключение - хвост " · монитор" у сайтов (monitor_link): его выбрал пользователь, и он
дописывается к уже готовой строке, мимо clean.
Полный текст (с подробностями) уходит в историю алертов и в панель, его не трогаем.
"""

from __future__ import annotations

import html
import re

LEVEL = {1: "⚠️", 2: "🔴", 3: "🚨"}
OK = "✅"
FACTS_MAX = 2
NAMES_MAX = 3

# что в чате заменяем: тире и кавычки на клавиатурные, стрелки словами
_SUBST = (
    (" — ", ": "), ("—", "-"), ("–", "-"), ("≥", ">="), ("≤", "<="), ("«", '"'), ("»", '"'),
    ("“", '"'), ("”", '"'), ("…", "..."), (" · ", ", "), ("·", ","), ("→", "->"), ("•", "-"),
    ("ё", "е"), ("Ё", "Е"), (" ", " "),
)


def clean(text: str) -> str:
    """Текст для чата: только символы с клавиатуры. Строки "↳ ..." (подсказки, хвосты логов)
    выбрасываются: это подробности для панели."""
    lines = [ln for ln in str(text or "").split("\n") if not ln.lstrip().startswith("↳")]
    out = "\n".join(lines)
    for a, b in _SUBST:
        out = out.replace(a, b)
    return out.replace("↳", "").strip()


def cap(s: str) -> str:
    """Заглавная - только у русского слова: "web перезапускается" остается с именем как есть."""
    s = str(s or "").strip()
    return s[:1].upper() + s[1:] if s and "а" <= s[0].lower() <= "я" else s


def low(s: str) -> str:
    """Первая буква строчная, если это не аббревиатура (CPU, RAID, DNS, OOM)."""
    s = str(s or "").strip()
    if len(s) > 1 and s[1].isupper() or not s or not "а" <= s[0].lower() <= "я":
        return s  # аббревиатура или имя латиницей (Redis, CPU) - как есть
    return s[:1].lower() + s[1:]


def names(items, limit: int = NAMES_MAX) -> str:
    """Перечень имен: больше limit - "и еще N". Повторы убираются, порядок сохраняется."""
    seen = list(dict.fromkeys(str(x) for x in items if str(x or "").strip()))
    if len(seen) <= limit:
        return ", ".join(seen)
    return ", ".join(seen[:limit]) + f" и еще {len(seen) - limit}"


def size(b: float | int | None) -> str:
    """Размер по-человечески, как пишет человек: 1гб, 58гб, 340мб, 1.2тб."""
    if b is None:
        return ""
    b = float(b)
    # от 1000 единиц - следующая: 1012мб это "1гб", а не "1012мб"
    for unit, word in ((1024 ** 4, "тб"), (1024 ** 3, "гб"), (1024 ** 2, "мб")):
        if b >= unit * 1000 / 1024:
            v = b / unit
            txt = f"{v:.1f}" if v < 10 else f"{v:.0f}"
            return txt.removesuffix(".0") + word
    return f"{max(b / 1024, 1):.0f}кб"


def mb(v: float | int | None) -> str:
    """Размер в мегабайтах -> по-человечески (1012 -> 1гб)."""
    return size(None if v is None else float(v) * 1024 ** 2)


def dur(sec: float | int | None) -> str:
    """Длительность: 6 с, 12 мин, 3 ч, 2 дня. Без минут при часах: точность чату не нужна."""
    if sec is None:
        return ""
    s = max(int(round(float(sec))), 0)
    if s < 60:
        return f"{s} с"
    if s < 3600:
        return f"{s // 60} мин"
    if s < 2 * 86400:
        return f"{s // 3600} ч"
    return days(s // 86400)


def days(n: int) -> str:
    n = int(n)
    if n % 10 == 1 and n % 100 != 11:
        w = "день"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        w = "дня"
    else:
        w = "дней"
    return f"{n} {w}"


def count(n: float | int | None) -> str:
    """Число по-человечески: 25 209 -> 25 тыс., 1 230 000 -> 1.2 млн."""
    if n is None:
        return ""
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} млн".replace(".0 млн", " млн")
    if n >= 10_000:
        return f"{n / 1000:.0f} тыс."
    return f"{n:.0f}"


def pct(v: float | int | None) -> str:
    if v is None:
        return ""
    v = float(v)
    return f"{v:.0f}%" if v >= 10 or v == int(v) else f"{v:.1f}%"


def fact(label: str, value: str) -> str:
    """Строка конкретики: "Убит: mariadbd". Без подписи - просто значение."""
    value = str(value or "").strip()
    return f"{cap(label)}: {value}" if label else value


def split_detail(detail: str) -> tuple[str, list[str]]:
    """Запасной разбор старого текста "что: подробности; еще одно": первая фраза до двоеточия -
    что случилось, остальное - одна строка конкретики. Для видов без своего короткого текста
    и для шаблонов, которые пользователь поменял сам."""
    body = clean(detail).split("\n")[0].strip()
    first = re.split(r";\s+", body)[0]
    what, sep, rest = first.partition(": ")
    if not sep:
        what, sep, rest = first.partition(" - ")
    more = len(re.split(r";\s+", body)) - 1
    facts = [rest.strip()] if rest.strip() else []
    if more > 0:
        tail = f"и еще {more}"
        facts = [f"{facts[0]}, {tail}"] if facts else [tail]
    return cap(what), facts


def render(icon: str, name_html: str, what: str, facts: list[str] | None = None) -> str:
    """Формат A в HTML Telegram. name_html - уже готовая ссылка или экранированное имя;
    what и facts - обычный текст, экранируются здесь."""
    lines = [f"{icon} {name_html}".strip(), f"<b>{html.escape(clean(cap(what)))}</b>"]
    # строки конкретики как есть: заглавная портила бы имена (caddy-proxy, ms-dev, tech1/...)
    for f in (facts or [])[:FACTS_MAX]:
        f = clean(f)
        if f:
            lines.append(html.escape(f))
    return "\n".join(lines)


def one_line(icon: str, name_html: str, what: str, facts: list[str] | None = None) -> str:
    """Тот же алерт одной строкой - для пачки: "⚠️💽 web-a: диск заполнен на 86%"."""
    out = f"{icon} {name_html}: {html.escape(clean(low(what)))}"
    f = next((clean(x) for x in facts or [] if clean(x)), "")
    if f:
        out += ", " + html.escape(low(f))
    return out


def recovery(name_html: str, what: str) -> str:
    """Отбой одной строкой: "✅ stats-a: снова на связи"."""
    return f"{OK} {name_html}: {html.escape(clean(low(what)))}"


def link(name: str, url: str = "") -> str:
    nm = html.escape(name)
    return f'<a href="{html.escape(url, quote=True)}">{nm}</a>' if url else nm


def monitor_link(url: str) -> str:
    """Хвост строки с именем сайта: " · монитор" ссылкой в монитор панели. Имя ведет на сам
    проверяемый сайт: "🔴🌐 example.com: не отвечает · монитор"."""
    return f" · {link('монитор', url)}" if url else ""
