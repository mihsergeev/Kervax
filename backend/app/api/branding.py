"""Свой логотип вместо стандартного (white-label).

Всё брендирование лежит в data_dir/branding — и картинка, и meta.json рядом с
ней. В БД не хранится ничего: бинарь раздувал бы каждый экспорт конфигурации,
а мета отдельно от файла давала бы полу-состояние — восстановили бэкап панели
на новой машине, мета говорит «логотип есть», а файла нет. Лежат вместе —
переносятся вместе (скопировать каталог), теряются тоже вместе.

Отдаём картинку ПУБЛИЧНО: логотип нужен на экране входа, то есть до всякой
авторизации. Логотип — не секрет, а вот загрузка и удаление — админские.
"""

import base64
import binascii
import hashlib
import json
import os
import re

from fastapi import APIRouter, HTTPException, Response, status

from app import audit
from app.config import get_settings
from app.deps import AdminUser, SessionDep
from app.schemas import BrandingIn, BrandingOut

router = APIRouter(prefix="/branding", tags=["branding"])

_META = "meta.json"
_MAX_BYTES = 512 * 1024  # 512 КБ: логотипу хватает, а память и бэкап не пухнут

# Тип определяем по СОДЕРЖИМОМУ, а не по Content-Type и не по расширению —
# и то, и другое задаёт загружающий.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
)

# В SVG исполняемого быть не должно. Через <img> скрипты внутри SVG браузер и так
# не выполняет, но файл отдаётся с нашего origin — режем на входе, чтобы он не стал
# опасен при любом другом способе показа.
_SVG_BAD = re.compile(
    rb"<\s*script|<\s*foreignObject|javascript:|\son\w+\s*=|<!ENTITY|<\s*iframe|<\s*use[^>]*href\s*=\s*[\"']\s*http",
    re.I,
)


def _dir() -> str:
    return os.path.join(get_settings().data_dir, "branding")


def _path(ext: str, dark: bool = False) -> str:
    return os.path.join(_dir(), f"logo-dark.{ext}" if dark else f"logo.{ext}")


_EXTS = ("png", "jpg", "gif", "webp", "svg")


def _decode(raw: str) -> bytes:
    """base64 или data-URL → байты с проверкой размера. Бросает 400/413."""
    raw = raw.strip()
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]  # «data:image/png;base64,AAA…» → «AAA…»
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Файл повреждён") from None
    if len(data) > _MAX_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"Файл больше {_MAX_BYTES // 1024} КБ — уменьшите логотип",
        )
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Пустой файл")
    return data


def _store(data: bytes, dark: bool, ext: str) -> None:
    """Кладёт уже проверенный файл, убирая прежний с другим расширением."""
    os.makedirs(_dir(), exist_ok=True)
    for stale in _EXTS:
        if stale != ext:
            try:
                os.remove(_path(stale, dark))
            except OSError:
                pass
    with open(_path(ext, dark), "wb") as f:
        f.write(data)


def _out(m: dict) -> BrandingOut:
    light = m.get("plate_light")
    return BrandingOut(
        logo=bool(m.get("ext")),
        title=m.get("title", ""),
        plate=m.get("plate", "auto"),
        plate_auto=bool(m.get("plate_auto")),
        plate_light=light if isinstance(light, bool) else None,
        dark_logo=bool(m.get("dark_ext")),
        dark_plate=bool(m.get("dark_plate")),
        version=m.get("v", 0),
    )


def _sniff(data: bytes) -> tuple[str, str]:
    """(media_type, расширение) по содержимому. Бросает 400 на всём остальном."""
    for magic, media, ext in _MAGIC:
        if data.startswith(magic):
            return media, ext
    head = data[:512].lstrip()
    if head.startswith(b"<?xml") or head.startswith(b"<svg") or b"<svg" in data[:1024].lower():
        if _SVG_BAD.search(data):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "В SVG есть скрипты или внешние ссылки — сохраните логотип без них "
                "или загрузите PNG",
            )
        return "image/svg+xml", "svg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST, "Нужен файл PNG, JPEG, WebP, GIF или SVG"
    )


def _read_meta() -> dict:
    """Мета из файла. Нет файла или он битый — считаем, что логотипа нет."""
    try:
        with open(os.path.join(_dir(), _META), encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_meta(meta: dict) -> None:
    os.makedirs(_dir(), exist_ok=True)
    path = os.path.join(_dir(), _META)
    # пишем через временный файл: оборванная запись не должна оставить обрубок,
    # из-за которого панель решит, что логотипа нет
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(tmp, path)


@router.get("", response_model=BrandingOut)
async def get_branding() -> BrandingOut:
    """Публично: экран входа рисуется до авторизации."""
    return _out(_read_meta())


@router.get("/logo")
async def get_logo() -> Response:
    return _serve(dark=False)


@router.get("/logo-dark")
async def get_logo_dark() -> Response:
    """Вариант для тёмной темы — тёмный логотип на тёмном фоне не виден, и вместо
    светлой плашки под ним лучше показать его светлую версию."""
    return _serve(dark=True)


def _serve(dark: bool) -> Response:
    m = _read_meta()
    ext = m.get("dark_ext") if dark else m.get("ext")
    media = m.get("dark_media") if dark else m.get("media")
    if not ext:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Логотип не задан")
    try:
        with open(_path(ext, dark), "rb") as f:
            data = f.read()
    except OSError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Логотип не задан") from None
    return Response(
        content=data,
        media_type=media or "application/octet-stream",
        headers={
            # Файл чужой: запрещаем всё, что он мог бы подтянуть, и запрет sniffing'а
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            "X-Content-Type-Options": "nosniff",
            # версия в URL меняется при замене — кэшируем надолго
            "Cache-Control": "public, max-age=604800",
        },
    )


@router.put("", response_model=BrandingOut)
async def put_logo(
    body: BrandingIn, user: AdminUser, session: SessionDep
) -> BrandingOut:
    """Логотип и его настройки. Файл (base64 или data-URL) необязателен, если логотип
    уже есть: раньше поменять подложку можно было, только заново выбрав тот же файл.
    plate: auto|always|never."""
    old = _read_meta()
    data = _decode(body.data) if body.data.strip() else None
    if data is None and not old.get("ext"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Выберите файл логотипа")
    dark = _decode(body.dark) if body.dark.strip() else None
    # оба файла проверяем ДО записи: иначе годный основной лёг бы на диск, а
    # отвергнутый вариант для тёмной темы оставил бы мету от прежнего файла
    kind = _sniff(data) if data is not None else None
    dark_kind = _sniff(dark) if dark is not None else None

    meta = dict(old)
    changed: list[str] = []
    if data is not None and kind is not None:
        meta["media"], meta["ext"] = kind
        _store(data, False, meta["ext"])
        meta["sha"] = hashlib.sha256(data).hexdigest()[:16]
        changed.append(f"{meta['ext']}, {len(data)} байт")
    if dark is not None and dark_kind is not None:
        meta["dark_media"], meta["dark_ext"] = dark_kind
        _store(dark, True, meta["dark_ext"])
        changed.append(f"тёмная тема: {meta['dark_ext']}, {len(dark)} байт")
    elif body.dark_remove and old.get("dark_ext"):
        for ext in _EXTS:
            try:
                os.remove(_path(ext, dark=True))
            except OSError:
                pass
        meta.pop("dark_ext", None)
        meta.pop("dark_media", None)
        changed.append("вариант для тёмной темы убран")

    meta["plate"] = body.plate if body.plate in ("auto", "always", "never") else "auto"
    meta["plate_auto"] = bool(body.plate_auto)
    if body.plate_light is not None:
        meta["plate_light"] = bool(body.plate_light)
    elif data is not None:
        meta.pop("plate_light", None)  # новый файл без анализа — пусть браузер решит сам
    meta["dark_plate"] = bool(body.dark_plate) if meta.get("dark_ext") else False
    meta["title"] = body.title.strip()[:64]
    # версия — чтобы браузер забрал новые файлы, не сбрасывая кэш вручную
    meta["v"] = int(old.get("v", 0)) + 1
    _write_meta(meta)
    await audit.record(
        session, user.username, "branding_set", "; ".join(changed) or "настройки"
    )
    return _out(meta)


@router.delete("", response_model=BrandingOut)
async def delete_logo(user: AdminUser, session: SessionDep) -> BrandingOut:
    m = _read_meta()
    for dark in (False, True):
        for ext in _EXTS:
            try:
                os.remove(_path(ext, dark))
            except OSError:
                pass
    meta = {"v": int(m.get("v", 0)) + 1, "title": m.get("title", "")}
    _write_meta(meta)
    await audit.record(session, user.username, "branding_clear", "")
    return _out(meta)
