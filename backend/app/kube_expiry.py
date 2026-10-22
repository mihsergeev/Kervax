"""Сроки кластера, за которыми не следим: "не нужен, не следить" у пункта раздела "Сроки".

Повод: старые TLS-секреты, которые давно никто не использует, истекли и навсегда красят раздел и
главную. Удалять секреты панель не может и не должна (она ничего не меняет в кластере), поэтому
человек отмечает пункт, а удалить его из кластера можно руками.

Отметка - не мьют вида алерта. Она привязана к тому, что видели, когда отмечали: ключ - вид и
место (namespace/имя или путь), к нему срок и отпечаток сертификата. Дальше:
- секрет обновили (другой срок или отпечаток) - отметка снимается в этом же тике, и за ним снова
  следим: новый сертификат тоже когда-нибудь истечет;
- секрет удалили из кластера - отметка тихо исчезает, но не сразу, а через GONE_S: helper мог
  разово не дочитать секреты, и решение человека из-за этого теряться не должно;
- кто и когда отметил - в записи, панель показывает это в подсказке.

Запись в servers.kube_expiry_ignored: {"<kind>|<where>": {by, at, expires, fp, gone_at?}}."""

from datetime import datetime

GONE_S = 86400
MAX_ITEMS = 500


def key(it: dict) -> str:
    return f"{it.get('kind') or ''}|{it.get('where') or ''}"


def _same(entry: dict, it: dict) -> bool:
    """Тот же сертификат, что отметили: срок совпал, отпечаток (если его знали оба раза) тоже."""
    if int(entry.get("expires") or 0) != int(it.get("expires") or 0):
        return False
    fp, cur = entry.get("fp") or "", it.get("fp") or ""
    return not fp or not cur or fp == cur


def _ignored(s) -> dict:
    v = getattr(s, "kube_expiry_ignored", None)
    return v if isinstance(v, dict) else {}


def active(s, rep: dict) -> dict[str, dict]:
    """Действующие отметки: пункт есть в отчете и это тот же сертификат."""
    ign = _ignored(s)
    if not ign:
        return {}
    out = {}
    for it in rep.get("kube_expiry") or []:
        k = key(it)
        e = ign.get(k)
        if isinstance(e, dict) and _same(e, it):
            out[k] = e
    return out


def live(s, rep: dict) -> list[dict]:
    """Сроки, за которыми следим: без отмеченных "не следить"."""
    items = rep.get("kube_expiry") or []
    act = active(s, rep)
    return [it for it in items if key(it) not in act] if act else list(items)


def view(s) -> dict[str, dict] | None:
    """Для карточки: кто и когда отметил, по ключу пункта. Только действующие отметки."""
    act = active(s, s.last_report or {})
    return {k: {"by": e.get("by") or "", "at": e.get("at") or ""} for k, e in act.items()} or None


def entry(it: dict, by: str, now: datetime) -> dict:
    return {"by": by, "at": now.isoformat(), "expires": int(it.get("expires") or 0), "fp": it.get("fp") or ""}


def step(ign: dict | None, rep: dict, now: datetime) -> dict[str, dict | None]:
    """Уборка отметок по свежему отчету: {ключ: новая запись или None - снять}; пусто - без
    изменений. Пустой список сроков не трогает ничего: это скорее сбой helper'а, чем удаленный
    разом весь кластер."""
    if not ign:
        return {}
    items = {key(it): it for it in rep.get("kube_expiry") or []}
    if not items:
        return {}
    out: dict[str, dict | None] = {}
    for k, e in ign.items():
        if not isinstance(e, dict):
            out[k] = None
            continue
        it = items.get(k)
        if it is not None:
            if not _same(e, it):
                out[k] = None  # обновили: следим за новым сертификатом
            elif e.get("gone_at"):
                out[k] = {x: v for x, v in e.items() if x != "gone_at"}
            continue
        try:
            gone = datetime.fromisoformat(str(e.get("gone_at") or ""))
        except ValueError:
            gone = None
        if gone is None:
            out[k] = {**e, "gone_at": now.isoformat()}
        elif (now - gone).total_seconds() >= GONE_S:
            out[k] = None
    return out

