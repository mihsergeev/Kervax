"""Ворклоад пода: деплоймент, стейтфулсет, демонсет.

Человек глушит то, что видит в списке, - под. А имя пода живет до следующего деплоя:
mail-worker-6b8b56c769-f4k8m после выкатки становится mail-worker-789cd65bb8-nkftn,
и заглушенные руками перезапуски снова пришли алертом. Поэтому мьюты и группировка - по
ворклоаду: из поля ctrl агента ("deployment/mail-worker"), а для старых мьютов, где
есть только имя пода, - по суффиксам, которые дописывает Kubernetes.
"""
from __future__ import annotations

import re

# Алфавит сгенерированных суффиксов Kubernetes (без гласных и похожих цифр)
_A = "bcdfghjklmnpqrstvwxz2456789"
_FROM_DEPLOYMENT = re.compile(rf"^(.+)-[{_A}]{{6,10}}-[{_A}]{{5}}$")
_FROM_DAEMONSET = re.compile(rf"^(.+)-[{_A}]{{5}}$")
_FROM_STATEFULSET = re.compile(r"^(.+)-\d+$")


def workload(name: str, ctrl: str = "") -> str:
    """Имя ворклоада пода: из ctrl агента, иначе имя пода без суффиксов Kubernetes."""
    if ctrl and "/" in ctrl:
        return ctrl.split("/", 1)[1]
    for rx in (_FROM_DEPLOYMENT, _FROM_DAEMONSET, _FROM_STATEFULSET):
        m = rx.match(name)
        if m:
            return m.group(1)
    return name


def pod_key(key: str, ctrl: str = "") -> str:
    """"pod:<ns>/<под>" -> "pod:<ns>/<ворклоад>", остальные ключи как есть."""
    if not key.startswith("pod:"):
        return key
    ns, _, name = key[4:].partition("/")
    return f"pod:{ns}/{workload(name, ctrl)}"


def ctrls(rep: dict | None) -> dict[str, str]:
    """"pod:<ns>/<под>" -> ctrl из отчета агента, у кого он есть."""
    out = {}
    for p in ((rep or {}).get("kube") or {}).get("pods") or []:
        if isinstance(p, dict) and p.get("name") and p.get("ctrl"):
            out[f"pod:{p.get('ns') or '?'}/{p['name']}"] = str(p["ctrl"])
    return out


def muted(keys: set[str], key: str, ctrl: str = "") -> bool:
    """Заглушен ли ключ: сам по себе или его ворклоад (мьют, поставленный на старый под)."""
    if key in keys:
        return True
    if not key.startswith("pod:"):
        return False
    wk = pod_key(key, ctrl)
    return wk in keys or any(pod_key(k) == wk for k in keys if k.startswith("pod:"))


def label(ns: str, name: str, ctrl: str = "") -> str:
    """Под для короткого алерта: ворклоад без хеша, неймспейс - только если он что-то добавляет.
    default/wordpress-mariadb-7d9f8b6c5d-x2k4q -> wordpress-mariadb, access-hub/access-hub-... ->
    access-hub, tech1/scheduler-dispatch-... -> tech1/scheduler-dispatch."""
    wl = workload(name, ctrl)
    return wl if not ns or ns in ("?", "default", wl) else f"{ns}/{wl}"
