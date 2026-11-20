"""Проверка сайта не съедает память и не висит: тело читается до лимита распакованных байт
(gzip-бомба), а весь запрос укладывается в общий срок (сервер, отдающий по байту)."""

import asyncio
import gzip
import time
import tracemalloc
import types
import zlib

import pytest

from app import check_dispatch
from app import checks as checks_exec

# падение на старом коде - явное, а не зависший прогон
_HANG = 15


@pytest.fixture(scope="module")
def bomb() -> bytes:
    """gzip: "needle" и 100 МБ нулей, ~100 КБ на проводе."""
    c = zlib.compressobj(9, zlib.DEFLATED, 31)
    zeros = bytes(1 << 20)
    parts = [c.compress(b"<html>needle ")]
    parts += [c.compress(zeros) for _ in range(100)]
    return b"".join(parts) + c.flush()


def _check(port: int, **kw) -> types.SimpleNamespace:
    base = dict(
        type="http", target=f"http://127.0.0.1:{port}/", timeout_ms=1000, degraded_ms=5000,
        method="GET", keyword_up="", keyword_down="", expected_status="200-399", retries=0,
    )
    return types.SimpleNamespace(**{**base, **kw})


async def _serve(handler):
    async def serve(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        try:
            await handler(reader, writer)
        except (ConnectionError, OSError):
            pass  # проба закрыла соединение - так и задумано
        finally:
            writer.close()

    return await asyncio.start_server(serve, "127.0.0.1", 0)


async def test_gzip_bomb_is_read_up_to_the_limit(bomb, monkeypatch):
    """Объявлено больше, чем пришло: проба, читающая тело целиком, ждала бы хвост до таймаута
    и держала бы в памяти 100 МБ распакованных нулей. Читаем только первые _MAX_BODY."""
    async def handler(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Encoding: gzip\r\n"
                     b"Content-Length: %d\r\n\r\n" % (len(bomb) + 1_000_000))
        for i in range(0, len(bomb), 4096):
            writer.write(bomb[i:i + 4096])
            await writer.drain()
        await reader.read()  # хвост не шлем: ждем, пока проба уйдет

    bodies = []
    real_eval = checks_exec._eval_http

    def spy(check, r, body, *a):
        bodies.append(len(body))
        return real_eval(check, r, body, *a)

    monkeypatch.setattr(checks_exec, "_eval_http", spy)
    server = await _serve(handler)
    async with server:
        port = server.sockets[0].getsockname()[1]
        tracemalloc.start()
        try:
            t0 = time.monotonic()
            out = await asyncio.wait_for(
                checks_exec.run_check(_check(port, timeout_ms=10000, keyword_up="needle")), _HANG)
            took = time.monotonic() - t0
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert out.status == "up", out.message
    assert bodies == [checks_exec._MAX_BODY]
    assert peak < 32 * 2**20, f"пик памяти {peak / 2**20:.0f} МБ"
    # up, а не таймаут: недосланный хвост не ждали (таймаут 10 с), чтение бросили на лимите
    assert took < 5


async def test_tamper_scan_is_bounded_too(bomb, monkeypatch):
    """Разбор признаков взлома качает ту же главную: тот же лимит тела и тот же общий срок."""
    from app import tamper

    monkeypatch.setattr(tamper, "_TIMEOUT", 1.0)
    page = zlib.compressobj(9, zlib.DEFLATED, 31)
    page = page.compress(b"<html><title>Shop</title>") + page.compress(bytes(50 << 20)) + page.flush()

    async def bomb_page(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Encoding: gzip\r\n"
                     b"Content-Length: %d\r\n\r\n" % (len(page) + 1000) + page)
        await writer.drain()
        await reader.read()

    async def drip(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 100\r\n\r\n")
        for _ in range(100):
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(0.3)

    for handler in (bomb_page, drip):
        server = await _serve(handler)
        async with server:
            port = server.sockets[0].getsockname()[1]
            chk = types.SimpleNamespace(target=f"http://127.0.0.1:{port}/", auth_method="", http_headers="",
                                        ignore_tls=False)
            t0 = time.monotonic()
            got = await asyncio.wait_for(tamper.scan(chk), _HANG)
            took = time.monotonic() - t0
        if handler is bomb_page:
            assert got["obs"]["size"] == tamper._MAX_BODY and got["obs"]["title"] == "Shop", got
        else:
            assert got == {"err": tamper._SCAN_TIMEOUT}
            assert 1.8 < took < 5


@pytest.mark.parametrize("where", ["headers", "body"])
async def test_drip_feeding_server_hits_the_deadline(where):
    """По байту раз в 0.3 с при таймауте 1 с: таймаут httpx на каждую операцию не срабатывает
    никогда, а общий срок запроса - через 2 таймаута."""
    async def handler(reader, writer):
        head = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 1000000\r\n\r\n"
        if where == "body":
            writer.write(head)
            head = b"x" * 1_000_000
        for b in head:
            writer.write(bytes([b]))
            await writer.drain()
            await asyncio.sleep(0.3)

    server = await _serve(handler)
    async with server:
        port = server.sockets[0].getsockname()[1]
        t0 = time.monotonic()
        out = await asyncio.wait_for(checks_exec.run_check(_check(port, keyword_up="x")), _HANG)
        took = time.monotonic() - t0
    assert out.status == "down"
    assert 1.8 < took < 5
    if where == "body":
        assert "не догрузился" in out.message, out.message
    else:
        assert "не ответил" in out.message, out.message


async def test_deadline_through_proxy_says_timeout():
    """Без фаз (через локацию) причина - из самого исключения."""
    async def handler(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
        for _ in range(100):
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(0.3)

    server = await _serve(handler)
    async with server:
        port = server.sockets[0].getsockname()[1]
        # прокси - сам тестовый сервер: на абсолютный URL он отвечает так же
        out = await asyncio.wait_for(checks_exec._run_http(
            _check(port, target="http://192.0.2.10/"), proxy=f"http://127.0.0.1:{port}"), _HANG)
    assert out.status == "down" and "время ожидания" in out.message, out.message


async def test_deadline_frees_origin_slots_and_skips_queue_time():
    """Очередь к origin (check_dispatch) в срок не входит, а после срыва срока permit возвращен."""
    async def fast(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()

    async def drip(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
        for _ in range(100):
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(0.3)

    for handler in (fast, drip):
        server = await _serve(handler)
        async with server:
            port = server.sockets[0].getsockname()[1]
            budget = check_dispatch.BatchBudget({})
            slots = budget.slots[("http", b"127.0.0.1", port)] = check_dispatch.OriginSlots()
            token = check_dispatch._REQUEST_SLOTS.set(budget)
            try:
                if handler is fast:
                    # все места заняты дольше срока запроса (2 с): ждем очередь, а не таймаут
                    for _ in range(check_dispatch._ORIGIN_CONCURRENCY):
                        await slots.concurrent.acquire()

                    async def release():
                        await asyncio.sleep(2.5)
                        for _ in range(check_dispatch._ORIGIN_CONCURRENCY):
                            slots.concurrent.release()

                    freeing = asyncio.create_task(release())
                    out = await asyncio.wait_for(checks_exec._run_http(_check(port)), _HANG)
                    await freeing
                    assert out.status == "up", out.message
                else:
                    out = await asyncio.wait_for(checks_exec._run_http(_check(port)), _HANG)
                    assert out.status == "down"
            finally:
                check_dispatch._REQUEST_SLOTS.reset(token)
            assert slots.concurrent._value == check_dispatch._ORIGIN_CONCURRENCY


@pytest.mark.parametrize("encoding", ["", "gzip", "deflate", "raw-deflate", "gzip, gzip"])
async def test_keyword_found_in_ordinary_and_compressed_pages(encoding):
    page = "<html><title>Example</title><body>" + "lorem ipsum " * 50_000 + "Welcome</body></html>"
    body = page.encode()
    header = encoding
    if encoding == "gzip":
        body = gzip.compress(body)
    elif encoding == "deflate":
        body = zlib.compress(body)
    elif encoding == "raw-deflate":
        c = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        body, header = c.compress(body) + c.flush(), "deflate"
    elif encoding == "gzip, gzip":
        body = gzip.compress(gzip.compress(body))

    async def handler(reader, writer):
        extra = f"Content-Encoding: {header}\r\n" if header else ""
        writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n{extra}"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()

    server = await _serve(handler)
    async with server:
        port = server.sockets[0].getsockname()[1]
        out = await asyncio.wait_for(checks_exec.run_check(_check(port, keyword_up="Welcome")), _HANG)
        assert out.status == "up", out.message
        out = await asyncio.wait_for(checks_exec.run_check(_check(port, keyword_up="Goodbye")), _HANG)
        assert out.status == "down" and "Goodbye" in out.message


@pytest.mark.parametrize("charset", ["windows-1251", ""])
async def test_cyrillic_keyword_in_windows_1251_page(charset):
    """Кодировка - из Content-Type, как у resp.text; без charset - utf-8 и слово не находится."""
    body = "<html><body>Добро пожаловать в магазин</body></html>".encode("cp1251")
    ctype = f"text/html; charset={charset}" if charset else "text/html"

    async def handler(reader, writer):
        writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()

    server = await _serve(handler)
    async with server:
        port = server.sockets[0].getsockname()[1]
        out = await asyncio.wait_for(
            checks_exec.run_check(_check(port, keyword_up="Добро пожаловать")), _HANG)
    assert out.status == ("up" if charset else "down"), out.message


# --- редиректы и распаковка с бюджетом ---

_MB = bytes(1 << 20)


def _flushed(head: bytes, piece: bytes, times: int) -> bytes:
    """gzip(head + piece * times) без концевика: после Z_FULL_FLUSH одинаковый вход дает
    одинаковый выход, поэтому гигабайты внутри собираются за миллисекунды."""
    c = zlib.compressobj(9, zlib.DEFLATED, 31)
    first = c.compress(head) + c.compress(piece) + c.flush(zlib.Z_FULL_FLUSH)
    again = c.compress(piece) + c.flush(zlib.Z_FULL_FLUSH)
    return first + again * (times - 1)


async def _measured(coro):
    """(результат, секунды, пик памяти) с явным падением вместо зависания."""
    tracemalloc.start()
    try:
        t0 = time.monotonic()
        out = await asyncio.wait_for(coro, _HANG)
        return out, time.monotonic() - t0, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


async def _serve_body(body: bytes, encoding: str):
    async def handler(reader, writer):
        writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Encoding: {encoding}\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode())
        view = memoryview(body)
        for i in range(0, len(body), 65536):
            writer.write(view[i:i + 65536])
            await writer.drain()

    return await _serve(handler)


async def test_redirect_body_is_not_read():
    """Тело 302 - двойной gzip со 128 МБ нулей: httpx с follow_redirects распаковал бы его целиком
    (aread на каждом хопе). Редирект пройден, тело 3xx не читали."""
    bomb = gzip.compress(_flushed(b"", _MB, 128))
    hops = []

    async def serve(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        path = head.split(b" ", 2)[1]
        hops.append(path)
        try:
            if path == b"/":
                writer.write(b"HTTP/1.1 302 Found\r\nLocation: /final\r\nContent-Encoding: gzip, gzip\r\n"
                             b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(bomb) + bomb)
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 13\r\nConnection: close\r\n\r\n"
                             b"needle inside")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        out, took, peak = await _measured(checks_exec.run_check(_check(port, keyword_up="needle")))
    assert out.status == "up", out.message
    assert hops == [b"/", b"/final"]
    assert peak < 32 * 2**20, f"пик памяти {peak / 2**20:.0f} МБ"
    assert took < 5


async def test_redirect_loop_is_still_too_many_redirects():
    async def handler(reader, writer):
        writer.write(b"HTTP/1.1 302 Found\r\nLocation: /again\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()

    server = await _serve(handler)
    async with server:
        port = server.sockets[0].getsockname()[1]
        out = await asyncio.wait_for(checks_exec.run_check(_check(port, timeout_ms=5000)), _HANG)
    assert out.status == "down" and "redirects" in out.message, out.message


_STORED = bytes.fromhex("000000ffff") * (len(_MB) // 5)  # пустые stored-блоки: вход есть, выхода нет
_GZ_HEAD = bytes.fromhex("1f8b08000000000000ff")
_CASES = {
    # (а) мусор после члена gzip: не копим его в unused_data завершенного объекта
    "garbage-after-member": (lambda: gzip.compress(b"<html>Welcome</html>") + _MB * 4, "gzip", "Welcome", "up"),
    # (б) внутри gzip(b"x"), дальше во внешнем слое - 128 МБ нулей
    "zeros-after-inner": (lambda: _flushed(gzip.compress(b"x"), _MB, 128), "gzip, gzip", "x", "up"),
    # (в) внутренний gzip из пустых stored-блоков (~4 МБ на проводе): выхода нет, работу
    # ограничивает бюджет
    "empty-stored-blocks": (lambda: _flushed(_GZ_HEAD, _STORED, 2560), "gzip, gzip", "Welcome", "down"),
    # (г) многочленный gzip: оба члена
    "two-members": (lambda: gzip.compress(b"Hello ") + gzip.compress(b"World"), "gzip", "World", "up"),
}


@pytest.mark.parametrize("case", list(_CASES))
async def test_decompression_has_a_budget(case):
    make, encoding, keyword, status = _CASES[case]
    server = await _serve_body(make(), encoding)
    async with server:
        port = server.sockets[0].getsockname()[1]
        out, took, peak = await _measured(
            checks_exec.run_check(_check(port, timeout_ms=10000, keyword_up=keyword)))
    assert out.status == status, out.message
    if status == "down":
        assert "нет ключевого слова" in out.message, out.message  # не таймаут и не ошибка
    assert took < 5
    assert peak < 48 * 2**20, f"пик памяти {peak / 2**20:.0f} МБ"


async def test_unknown_text_codec_falls_back_to_utf8():
    """charset=rot13 - кодек есть, но не текстовый: bytes.decode бросает LookupError."""
    body = "<html>Добро пожаловать</html>".encode()

    async def handler(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=rot13\r\n"
                     b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
        await writer.drain()

    server = await _serve(handler)
    async with server:
        port = server.sockets[0].getsockname()[1]
        out = await asyncio.wait_for(checks_exec.run_check(_check(port, keyword_up="Добро пожаловать")), _HANG)
    assert out.status == "up", out.message


async def _drip_on(*hosts: str):
    """Сервер, отдающий по байту раз в 0.3 с, на одном порту на нескольких адресах."""
    async def serve(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        try:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
            for _ in range(100):
                writer.write(b"x")
                await writer.drain()
                await asyncio.sleep(0.3)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    servers = [await asyncio.start_server(serve, hosts[0], 0)]
    port = servers[0].sockets[0].getsockname()[1]
    for host in hosts[1:]:
        servers.append(await asyncio.start_server(serve, host, port))
    return servers, port


async def test_whole_monitor_has_a_budget(monkeypatch):
    """retries=1 и "все адреса" на сервер, отдающий по байту; повтор зависает вне HTTP-запросов,
    и срок запроса его не ограничивает. Срок монитора - 2 x (3 + 2) с = 10 с (вторая волна адресов
    в запас), результат - первая попытка с пометкой. Адреса разные (127.0.0.1 и 127.0.0.2):
    одинаковые net_guard.classified_addresses сводит в один, и "все адреса" выродились бы в одну
    проверку."""
    from app import net_guard

    async def resolve(host, timeout):
        return ["127.0.0.1", "127.0.0.2"]

    real = checks_exec._run_http_all_ips
    calls = []

    async def hang_on_retry(*a, **kw):
        calls.append(1)
        if len(calls) > 1:
            await asyncio.sleep(60)
        return await real(*a, **kw)

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(checks_exec, "_run_http_all_ips", hang_on_retry)
    monkeypatch.setattr(checks_exec, "get_settings", lambda: types.SimpleNamespace(check_retry_delay_ms=0))
    try:
        servers, port = await _drip_on("127.0.0.1", "127.0.0.2")
    except OSError as exc:
        pytest.skip(f"второй адрес loopback недоступен: {exc}")
    try:
        chk = _check(port, target=f"http://drip.example.test:{port}/", retries=1, check_all_ips=True)
        t0 = time.monotonic()
        out = await asyncio.wait_for(checks_exec.run_check(chk), 30)
        took = time.monotonic() - t0
    finally:
        for s in servers:
            s.close()
    assert out.status == "down"
    assert "не уложилась в 10 с" in out.message, out.message
    assert "0/2 адресов OK" in out.message and len(out.ip_results) == 2, out.message
    assert 9.5 < took < 10 + 3, took


async def test_budget_keeps_the_last_finished_attempt(monkeypatch):
    """Попытка 1 - HTTP 503, попытка 2 зависла (DNS-замер на минуту): срок вышел - отдаем
    попытку 1 с пометкой, а не безликий таймаут."""
    calls = []

    async def slow_second(url, timeout):
        calls.append(url)
        if len(calls) > 1:
            await asyncio.sleep(60)
        return None, True

    async def handler(reader, writer):
        writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()

    monkeypatch.setattr(checks_exec, "_dns_time", slow_second)
    server = await _serve(handler)
    async with server:
        port = server.sockets[0].getsockname()[1]
        out = await asyncio.wait_for(checks_exec.run_check(_check(port, retries=1)), 30)
    assert out.status == "down"
    assert "HTTP 503" in out.message and "повтор прерван" in out.message, out.message
    assert out.phases is not None


async def test_queue_at_origin_is_not_monitor_time():
    """Все места к origin заняты дольше срока монитора (3 с): монитор ждет очередь, а не
    падает по сроку. Счетчик очереди - в объекте срока, а не в ContextVar."""
    async def fast(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()

    server = await _serve(fast)
    async with server:
        port = server.sockets[0].getsockname()[1]
        budget = check_dispatch.BatchBudget({})
        slots = budget.slots[("http", b"127.0.0.1", port)] = check_dispatch.OriginSlots()
        for _ in range(check_dispatch._ORIGIN_CONCURRENCY):
            await slots.concurrent.acquire()

        async def release():
            await asyncio.sleep(4)
            for _ in range(check_dispatch._ORIGIN_CONCURRENCY):
                slots.concurrent.release()

        token = check_dispatch._REQUEST_SLOTS.set(budget)
        try:
            freeing = asyncio.create_task(release())
            out = await asyncio.wait_for(checks_exec.run_check(_check(port)), _HANG)
            await freeing
        finally:
            check_dispatch._REQUEST_SLOTS.reset(token)
    assert out.status == "up", out.message


async def _max_gap(coro) -> tuple[object, float, float]:
    """(результат, секунды, самая долгая пауза цикла событий): тикер рядом с пробой."""
    gaps = [0.0]
    stop = False

    async def tick():
        last = time.monotonic()
        while not stop:
            await asyncio.sleep(0.005)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(tick())
    t0 = time.monotonic()
    try:
        out = await asyncio.wait_for(coro, _HANG)
    finally:
        stop = True
        await ticker
    return out, time.monotonic() - t0, max(gaps)


_EMPTY = gzip.compress(b"")


@pytest.mark.parametrize("encoding", ["gzip", "gzip, gzip"])
async def test_empty_gzip_members_do_not_block_the_loop(encoding):
    """Пустой член gzip - ~20 байт без выхода. 10^5 членов в одном слое или внешний слой,
    выдающий ~8 МБ пустых членов: на каждом члене новый decompressobj и копия остатка
    давали бы секунды синхронной работы. Членов на слой - не больше 32, работа - с уступками."""
    if encoding == "gzip":
        body = _EMPTY * 100_000 + gzip.compress(b"Welcome")
    else:
        body = _flushed(b"", _EMPTY * (len(_MB) // len(_EMPTY)), 8)
    server = await _serve_body(body, encoding)
    async with server:
        port = server.sockets[0].getsockname()[1]
        out, took, gap = await _max_gap(checks_exec.run_check(_check(port, timeout_ms=10000, keyword_up="Welcome")))
    assert out.status == "down" and "нет ключевого слова" in out.message, out.message
    assert took < 3
    assert gap < 0.5, f"цикл событий стоял {gap:.2f} с"


async def test_inflate_yields_to_the_loop():
    """Обычная распаковка 2 МБ через два слоя тоже отдает ход циклу."""
    body = gzip.compress(gzip.compress(b"Welcome " + bytes(8 << 20)))
    server = await _serve_body(body, "gzip, gzip")
    async with server:
        port = server.sockets[0].getsockname()[1]
        out, took, gap = await _max_gap(checks_exec.run_check(_check(port, timeout_ms=10000, keyword_up="Welcome")))
    assert out.status == "up", out.message
    assert gap < 0.5, f"цикл событий стоял {gap:.2f} с"


async def test_tamper_scan_has_a_total_budget(monkeypatch):
    """Страница быстрая, а 15 своих скриптов отдаются по байту: у каждого свой срок (2 с), но
    весь скан - не дольше 3 x _TIMEOUT. Итог - как сетевая ошибка, без признаков."""
    from app import tamper

    monkeypatch.setattr(tamper, "_TIMEOUT", 1.0)
    page = ("<html><head><title>Shop</title>"
            + "".join(f'<script src="/s{i}.js"></script>' for i in range(15))
            + "</head><body>ok</body></html>").encode()

    async def serve(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        try:
            if head.split(b" ", 2)[1] == b"/":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n"
                             b"Connection: close\r\n\r\n" % len(page) + page)
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
            for _ in range(100):
                writer.write(b"x")
                await writer.drain()
                await asyncio.sleep(0.3)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        chk = types.SimpleNamespace(target=f"http://127.0.0.1:{port}/", auth_method="", http_headers="",
                                    ignore_tls=False)
        t0 = time.monotonic()
        got = await asyncio.wait_for(tamper.scan(chk), 40)
        took = time.monotonic() - t0
    assert got == {"err": tamper._SCAN_TIMEOUT}
    assert 2.5 < took < 5, took


def test_tamper_cut_page_is_not_an_emptied_page():
    """Главная длиннее лимита чтения: размер обрезан, с базой (в т.ч. записанной до лимита) не
    сравниваем. Честно маленькая главная - по-прежнему признак."""
    from datetime import datetime, timezone

    from app import tamper

    now = datetime(2026, 9, 25, 19, 0, tzinfo=timezone.utc)
    st = {"base": {"title": "Shop", "size": 15_000_000, "domains": {}, "at": now.isoformat()}}
    obs = {"size": tamper._MAX_BODY, "title": "Shop", "domains": {}, "bot": None, "engine": {}}
    got = tamper.observe(st, obs, now, set(), {})
    assert "empty" not in got["pend"] and "empty" not in got["signs"]
    got = tamper.observe(st, dict(obs, size=100_000), now, set(), {})
    assert "empty" in got["pend"]


# --- кодировки, потолок срока, очередь, пути ---

def test_punycode_charset_is_decoded_as_utf8_fast():
    """charset=punycode: кодек есть в codecs, но квадратичен (2 МБ - десятки секунд синхронно)."""
    import httpx

    body = b"Welcome-" + b"a" * 2_000_000
    resp = httpx.Response(200, headers={"content-type": "text/html; charset=punycode"}, content=body)
    t0 = time.monotonic()
    text = checks_exec._body_text(resp, body)
    assert time.monotonic() - t0 < 1
    assert text.startswith("Welcome-")


@pytest.mark.parametrize("charset", ["idna", "punycode"])
async def test_odd_charset_page_is_searched_as_utf8(charset, monkeypatch):
    """charset=idna бросал UnicodeError мимо обработки: монитор - down, tamper - "ошибка разбора"."""
    from app import tamper

    body = "<html><title>Магазин</title><body>Добро пожаловать</body></html>".encode()

    async def handler(reader, writer):
        writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset={charset}\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()

    server = await _serve(handler)
    async with server:
        port = server.sockets[0].getsockname()[1]
        out = await asyncio.wait_for(checks_exec.run_check(_check(port, keyword_up="Добро пожаловать")), _HANG)
        chk = types.SimpleNamespace(target=f"http://127.0.0.1:{port}/", auth_method="", http_headers="",
                                    ignore_tls=False)
        got = await asyncio.wait_for(tamper.scan(chk), _HANG)
    assert out.status == "up", out.message
    assert got["obs"]["title"] == "Магазин", got


def _gated(port: int):
    budget = check_dispatch.BatchBudget({})
    slots = budget.slots[("http", b"127.0.0.1", port)] = check_dispatch.OriginSlots()
    return budget, slots


async def test_queue_cannot_hold_a_monitor_past_the_hard_ceiling():
    """Места к origin заняты чужими все время: срок на очередь стоит, но потолок (2 x 3 с) -
    нет. Монитор завершается, а не висит."""
    async def fast(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()

    server = await _serve(fast)
    async with server:
        port = server.sockets[0].getsockname()[1]
        budget, slots = _gated(port)
        for _ in range(check_dispatch._ORIGIN_CONCURRENCY):
            await slots.concurrent.acquire()
        token = check_dispatch._REQUEST_SLOTS.set(budget)
        try:
            t0 = time.monotonic()
            out = await asyncio.wait_for(checks_exec.run_check(_check(port)), _HANG)
            took = time.monotonic() - t0
        finally:
            check_dispatch._REQUEST_SLOTS.reset(token)
    assert out.status == "down" and "с очередью" in out.message, out.message
    assert 5.5 < took < 8, took
    # отмененный запрос из очереди permit не унес
    assert slots.concurrent._value == 0


async def test_monitor_does_not_queue_behind_itself():
    """20 живых, но медленных путей (1.5 с) на один origin при 4 местах: пути ждут своей квоты
    (это время монитора, а не пауза очереди), а срок монитора учитывает их волны. Итог - вердикт
    сайта (up), а не "не уложилась"."""
    async def serve(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        try:
            if head.split(b" ", 2)[1] != b"/":
                await asyncio.sleep(1.5)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        budget, slots = _gated(port)
        token = check_dispatch._REQUEST_SLOTS.set(budget)
        try:
            t0 = time.monotonic()
            out = await asyncio.wait_for(checks_exec.run_check(
                _check(port, timeout_ms=2000, extra_paths=[f"/p{i}" for i in range(20)])), 40)
            took = time.monotonic() - t0
        finally:
            check_dispatch._REQUEST_SLOTS.reset(token)
    assert out.status == "up", out.message
    assert len(out.path_results) == 20
    assert 7 < took < 20, took
    assert slots.concurrent._value == check_dispatch._ORIGIN_CONCURRENCY


@pytest.mark.parametrize("charset,text", [("gb2312", "欢迎光临"), ("cp932", "ようこそ"),
                                          ("iso-8859-9", "Hoş geldiniz"), ("utf-8-sig", "Добро пожаловать")])
async def test_common_charsets_still_work(charset, text):
    """Частые кодировки вне меток WHATWG (gb2312, cp932, iso-8859-9, utf-8-sig) - как раньше."""
    body = f"<html><body>{text}</body></html>".encode(charset)

    async def handler(reader, writer):
        writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset={charset}\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()

    server = await _serve(handler)
    async with server:
        port = server.sockets[0].getsockname()[1]
        out = await asyncio.wait_for(checks_exec.run_check(_check(port, keyword_up=text)), _HANG)
    assert out.status == "up", out.message


async def test_byte_sized_chunks_do_not_block_the_loop():
    """2 МБ однобайтовыми чанками по plain http: каждое событие h11 - отдельный кусок. Кусок стоит
    не меньше 4 КБ работы (уступки циклу), кусков - не больше 16384 (дальше - как лимит)."""
    wire = b"7\r\nWelcome\r\n" + b"1\r\nX\r\n" * 2_000_000 + b"0\r\n\r\n"

    async def handler(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nTransfer-Encoding: chunked\r\n\r\n")
        view = memoryview(wire)
        for i in range(0, len(view), 65536):
            writer.write(view[i:i + 65536])
            await writer.drain()

    server = await _serve(handler)
    async with server:
        port = server.sockets[0].getsockname()[1]
        out, took, gap = await _max_gap(checks_exec.run_check(_check(port, timeout_ms=10000, keyword_up="Welcome")))
    assert out.status == "up", out.message
    assert took < 5, took
    assert gap < 0.5, f"цикл событий стоял {gap:.2f} с"


async def test_cancel_in_gate_queue_unfreezes_the_budget(monkeypatch):
    """Отмена задачи, стоящей в очереди ворот: пауза срока снята (queued == 0), permit не унесен."""
    made = []

    class Spy(checks_exec._Budget):
        def __init__(self, cm):
            super().__init__(cm)
            made.append(self)

    monkeypatch.setattr(checks_exec, "_Budget", Spy)
    budget, slots = _gated(9)
    for _ in range(check_dispatch._ORIGIN_CONCURRENCY):
        await slots.concurrent.acquire()
    token = check_dispatch._REQUEST_SLOTS.set(budget)
    try:
        task = asyncio.create_task(checks_exec.run_check(_check(9, timeout_ms=10000)))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if made and made[0].queued:
                break
        assert made and made[0].queued == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        check_dispatch._REQUEST_SLOTS.reset(token)
    assert made[0].queued == 0
    assert slots.concurrent._value == 0


async def test_paths_read_only_a_small_body(monkeypatch):
    """У путей важен код: тело - не больше 64 КБ и без декодирования. Главная - как раньше."""
    page = b"<html>Welcome</html>" + b"x" * 1_000_000
    read = []
    real = checks_exec._read_body

    async def spy(resp, limit=checks_exec._MAX_BODY):
        body = await real(resp, limit)
        read.append((resp.request.url.path, len(body)))
        return body

    async def handler(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n"
                     b"Connection: close\r\n\r\n" % len(page) + page)
        await writer.drain()

    monkeypatch.setattr(checks_exec, "_read_body", spy)
    server = await _serve(handler)
    async with server:
        port = server.sockets[0].getsockname()[1]
        out = await asyncio.wait_for(
            checks_exec.run_check(_check(port, keyword_up="Welcome", extra_paths=["/health"])), _HANG)
    assert out.status == "up", out.message
    assert sorted(read) == [("/", len(page)), ("/health", 64 * 1024)]
