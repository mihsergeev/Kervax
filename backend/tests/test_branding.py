"""Свой логотип: что принимается, что отвергается и кому это доступно."""

import base64

import httpx

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg=="
)
SVG_OK = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>'
SVG_SCRIPT = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
SVG_ONLOAD = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><rect/></svg>'


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def _put(client, headers, data: bytes, **extra):
    body = {"data": b64(data), "plate": "auto", "title": "", **extra}
    return await client.put("/api/branding", json=body, headers=headers)


async def test_branding_default_is_empty(client: httpx.AsyncClient):
    # без токена: экран входа рисуется до авторизации, значит ручка публичная
    r = await client.get("/api/branding")
    assert r.status_code == 200
    assert r.json()["logo"] is False
    assert (await client.get("/api/branding/logo")).status_code == 404


async def test_branding_png_roundtrip(client: httpx.AsyncClient, auth_headers):
    r = await _put(client, auth_headers, PNG_1PX, title="Ромашка", plate_auto=True)
    assert r.status_code == 200, r.text
    assert r.json()["logo"] is True and r.json()["title"] == "Ромашка"
    assert r.json()["plate_auto"] is True

    meta = (await client.get("/api/branding")).json()
    assert meta["logo"] is True and meta["version"] >= 1

    logo = await client.get("/api/branding/logo")
    assert logo.status_code == 200
    assert logo.headers["content-type"].startswith("image/png")
    # чужой файл отдаём с запретом на подгрузку чего-либо и без sniffing'а
    assert "default-src 'none'" in logo.headers["content-security-policy"]
    assert logo.headers["x-content-type-options"] == "nosniff"
    assert logo.content == PNG_1PX

    r = await client.delete("/api/branding", headers=auth_headers)
    assert r.status_code == 200 and r.json()["logo"] is False
    assert (await client.get("/api/branding/logo")).status_code == 404


async def test_branding_accepts_clean_svg(client: httpx.AsyncClient, auth_headers):
    r = await _put(client, auth_headers, SVG_OK)
    assert r.status_code == 200
    logo = await client.get("/api/branding/logo")
    assert logo.headers["content-type"].startswith("image/svg+xml")


async def test_branding_rejects_active_svg(client: httpx.AsyncClient, auth_headers):
    # SVG с <script> и с on*-атрибутом: файл отдаётся с нашего origin, поэтому
    # активное содержимое режем на входе, а не надеемся на способ показа
    for payload in (SVG_SCRIPT, SVG_ONLOAD):
        r = await _put(client, auth_headers, payload)
        assert r.status_code == 400, payload
    assert (await client.get("/api/branding")).json()["logo"] is False


async def test_branding_rejects_non_image(client: httpx.AsyncClient, auth_headers):
    # тип определяется по содержимому: назваться картинкой недостаточно
    r = await _put(client, auth_headers, b"#!/bin/sh\nrm -rf /\n")
    assert r.status_code == 400


async def test_branding_rejects_oversize(client: httpx.AsyncClient, auth_headers):
    r = await _put(client, auth_headers, PNG_1PX + b"\x00" * (512 * 1024))
    assert r.status_code == 413


async def test_branding_write_requires_admin(client: httpx.AsyncClient, auth_headers):
    created = await client.post(
        "/api/users",
        json={"username": "brandeditor", "password": "editorpass-001", "role": "editor"},
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    token = (
        await client.post(
            "/api/auth/login",
            json={"username": "brandeditor", "password": "editorpass-001"},
        )
    ).json()["access_token"]
    editor = {"Authorization": f"Bearer {token}"}

    assert (await client.get("/api/branding", headers=editor)).status_code == 200
    assert (await _put(client, editor, PNG_1PX)).status_code == 403
    assert (await client.delete("/api/branding", headers=editor)).status_code == 403


async def test_branding_plate_per_theme_and_dark_variant(client: httpx.AsyncClient, auth_headers):
    """Тёмный логотип на прозрачном фоне: плашка нужна только на тёмной теме.

    Живой случай — логотип corpsoft24 (почти чёрный текст): панель подкладывала белую
    плашку и на светлой теме, где он и так виден. На тёмной вместо плашки можно
    показать светлый вариант — отдельным файлом.
    """
    r = await _put(client, auth_headers, SVG_OK, plate_auto=True, plate_light=False)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["plate_auto"] is True and b["plate_light"] is False and b["dark_logo"] is False
    assert (await client.get("/api/branding/logo-dark")).status_code == 404

    # вариант для тёмной темы — без повторной заливки основного файла
    r = await client.put("/api/branding", headers=auth_headers, json={
        "plate": "auto", "plate_auto": True, "plate_light": False, "title": "",
        "dark": b64(PNG_1PX), "dark_plate": False,
    })
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["logo"] is True and b["dark_logo"] is True and b["version"] == 2
    dark = await client.get("/api/branding/logo-dark")
    assert dark.status_code == 200 and dark.content == PNG_1PX
    assert "default-src 'none'" in dark.headers["content-security-policy"]
    # основной файл остался прежним
    assert (await client.get("/api/branding/logo")).content == SVG_OK

    # только настройки: подложку можно поменять, не выбирая файл заново
    r = await client.put("/api/branding", headers=auth_headers, json={
        "plate": "never", "plate_auto": True, "plate_light": False, "title": "Ромашка",
        "dark_plate": False,
    })
    assert r.status_code == 200 and r.json()["plate"] == "never"
    assert r.json()["dark_logo"] is True and r.json()["title"] == "Ромашка"

    # активный SVG не проходит и как вариант для тёмной темы
    r = await client.put("/api/branding", headers=auth_headers, json={
        "plate": "auto", "title": "", "dark": b64(SVG_SCRIPT),
    })
    assert r.status_code == 400

    # убрать вариант для тёмной темы
    r = await client.put("/api/branding", headers=auth_headers, json={
        "plate": "auto", "plate_auto": True, "title": "", "dark_remove": True,
    })
    assert r.json()["dark_logo"] is False
    assert (await client.get("/api/branding/logo-dark")).status_code == 404

    # сброс убирает оба файла
    await client.put("/api/branding", headers=auth_headers, json={
        "plate": "auto", "title": "", "dark": b64(PNG_1PX),
    })
    r = await client.delete("/api/branding", headers=auth_headers)
    assert r.json()["logo"] is False and r.json()["dark_logo"] is False
    assert (await client.get("/api/branding/logo-dark")).status_code == 404


async def test_branding_settings_need_a_logo(client: httpx.AsyncClient, auth_headers):
    # без файла сохранять нечего: настройки без логотипа ничего не значат
    r = await client.put("/api/branding", headers=auth_headers,
                         json={"plate": "auto", "title": ""})
    assert r.status_code == 400
