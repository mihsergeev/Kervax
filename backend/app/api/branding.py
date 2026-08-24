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


def _path(ext: str) -> str:
    return os.path.join(_dir(), f"logo.{ext}")


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
    m = _read_meta()
    return BrandingOut(
        logo=bool(m.get("ext")),
        title=m.get("title", ""),
        plate=m.get("plate", "auto"),
        plate_auto=bool(m.get("plate_auto")),
        version=m.get("v", 0),
    )


@router.get("/logo")
async def get_logo() -> Response:
    m = _read_meta()
    ext, media = m.get("ext"), m.get("media")
    if not ext:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Логотип не задан")
    try:
        with open(_path(ext), "rb") as f:
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
    """Заливает логотип (base64 или data-URL). plate: auto|always|never."""
    raw = body.data.strip()
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
    media, ext = _sniff(data)
    plate = body.plate if body.plate in ("auto", "always", "never") else "auto"
    title = body.title

    os.makedirs(_dir(), exist_ok=True)
    old = _read_meta()
    for stale in {"png", "jpg", "gif", "webp", "svg"}:
        if stale != ext:
            try:
                os.remove(_path(stale))
            except OSError:
                pass
    with open(_path(ext), "wb") as f:
        f.write(data)

    meta = {
        "ext": ext,
        "media": media,
        "plate": plate,
        "plate_auto": bool(body.plate_auto),
        "title": title.strip()[:64],
        # версия — чтобы браузер забрал новый файл, не сбрасывая кэш вручную
        "v": int(old.get("v", 0)) + 1,
        "sha": hashlib.sha256(data).hexdigest()[:16],
    }
    _write_meta(meta)
    await audit.record(session, user.username, "branding_set", f"{ext}, {len(data)} байт")
    return BrandingOut(logo=True, title=meta["title"], plate=plate,
                       plate_auto=bool(body.plate_auto), version=meta["v"])


@router.delete("", response_model=BrandingOut)
async def delete_logo(user: AdminUser, session: SessionDep) -> BrandingOut:
    m = _read_meta()
    if m.get("ext"):
        try:
            os.remove(_path(m["ext"]))
        except OSError:
            pass
    _write_meta({"v": int(m.get("v", 0)) + 1, "title": m.get("title", "")})
    await audit.record(session, user.username, "branding_clear", "")
    return BrandingOut(logo=False, title=m.get("title", ""), plate="auto",
                       plate_auto=False, version=int(m.get("v", 0)) + 1)
