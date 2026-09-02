"""Исполнители мониторов: http / tcp_port / cert — выполняются с самой панели.

Каждый возвращает CheckOutcome (status up|degraded|down + latency/value/message).
Никаких SSH — только сеть (httpx / сокет / TLS).
"""

import asyncio
import math
import json
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from app.config import get_settings

# верхняя граница читаемого тела при поиске ключевого слова (защита от гигантских ответов)
_MAX_BODY = 2_000_000

# браузероподобный User-Agent — многие сайты режут запросы без него (403/429),
# из-за чего мониторинг ложно показывал бы «down»
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 KervaxMonitor/1.0"
)


@dataclass
class CheckOutcome:
    status: str  # up | degraded | down
    latency_ms: int | None = None
    value: float | None = None  # напр. дней до истечения сертификата
    message: str = ""
    # разбивка по IP (режим «проверять все адреса»): [{ip,status,latency_ms,message}]
    ip_results: list | None = None


def status_matches(code: int, spec: str) -> bool:
    """Проверяет HTTP-код против спеки вида '200-399' или '200-299,301,302'."""
    spec = (spec or "").strip() or "200-399"
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, _, hi = part.partition("-")
            if lo.strip().isdigit() and hi.strip().isdigit():
                if int(lo) <= code <= int(hi):
                    return True
        elif part.isdigit() and int(part) == code:
            return True
    return False


def _cert_days_left(not_after: str) -> float:
    """Дней до истечения из notAfter (формат OpenSSL 'Jun  1 12:00:00 2026 GMT')."""
    expires = ssl.cert_time_to_seconds(not_after)
    now = datetime.now(timezone.utc).timestamp()
    return (expires - now) / 86400.0


async def _ssl_probe(host: str, port: int, timeout: float) -> tuple[float | None, str]:
    """TLS-хендшейк с проверкой цепочки. Возвращает (дней_до_истечения|None, сообщение).
    None + сообщение = сертификат невалиден/недоступен (истёк, mismatch, self-signed…)."""
    ctx = ssl.create_default_context()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=host), timeout
        )
    except ssl.SSLCertVerificationError as exc:
        return None, _short(f"сертификат невалиден: {exc.verify_message or exc}")
    except ssl.SSLError as exc:
        return None, _short(f"ошибка TLS: {exc}")
    except asyncio.TimeoutError:
        return None, "сайт недоступен (таймаут)"
    except OSError:
        return None, "сайт недоступен"  # DNS/сеть — SSL не проверить, пока сайт лежит
    try:
        cert = writer.get_extra_info("ssl_object").getpeercert()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    not_after = cert.get("notAfter") if cert else None
    if not not_after:
        return None, "не удалось прочитать срок сертификата"
    return _cert_days_left(not_after), "ok"


# двухуровневые TLD, где регистрируемый домен — три последних метки
_MULTI_TLD = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "com.au", "net.au", "org.au",
    "co.nz", "com.cy", "co.il", "com.br", "com.tr", "co.jp", "com.sg", "com.hk",
    "com.ua", "co.za", "com.mx", "com.ar",
}


def _registrable_domain(target: str) -> str:
    host = target.strip()
    if "://" in host:
        host = urlparse(host).hostname or host
    host = host.split("/")[0].split(":")[0].strip().rstrip(".").lower()
    labels = [x for x in host.split(".") if x]
    if len(labels) <= 2:
        return ".".join(labels)
    last2 = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if last2 in _MULTI_TLD else last2


def _parse_date(s: str) -> datetime | None:
    """Парсит дату истечения из RDAP/WHOIS: ISO8601 (часто с Z) или 'YYYY.MM.DD'."""
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(s[:10], "%Y.%m.%d")
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _days_until(dt: datetime) -> int:
    """Полных суток до даты; отрицательное — уже прошла.

    Именно floor, а не int(): int усекает К НУЛЮ, поэтому дата, прошедшая пару
    часов назад, давала 0 — тот же результат, что и «истекает через 20 часов».
    Отличить «ещё не истекло» от «уже истекло» по такому числу невозможно."""
    return math.floor((dt - datetime.now(timezone.utc)).total_seconds() / 86400.0)


# зоны регистратуры RU-CENTER/TCI — RDAP нет, но есть авторитетный WHOIS
_RU_ZONES = (".ru", ".su", ".рф", ".xn--p1ai")
_RU_WHOIS = "whois.tcinet.ru"


def _to_ascii(domain: str) -> str:
    """IDN → punycode (для .рф и др. кириллических доменов)."""
    if domain.isascii():
        return domain
    try:
        return domain.encode("idna").decode("ascii")
    except Exception:  # noqa: BLE001 — на кривом IDN отдаём как есть
        return domain


async def _domain_days_rdap(domain: str, timeout: float) -> tuple[int | None, str]:
    """Дней до истечения регистрации домена через RDAP (rdap.org — bootstrap)."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(f"https://rdap.org/domain/{domain}")
    except httpx.HTTPError:
        return None, "не удалось проверить срок домена"
    if r.status_code == 404:
        return None, "нет данных RDAP"
    if r.status_code != 200:
        return None, f"RDAP HTTP {r.status_code}"
    try:
        events = r.json().get("events", [])
    except ValueError:
        return None, "RDAP: некорректный ответ"
    exp = next(
        (e.get("eventDate") for e in events if e.get("eventAction") == "expiration"),
        None,
    )
    if not exp:
        return None, "нет даты истечения"
    dt = _parse_date(exp)
    return (_days_until(dt), "ok") if dt else (None, "не разобрать дату")


async def _domain_days_whois_ru(domain: str, timeout: float) -> tuple[int | None, str]:
    """Дней до истечения для .ru/.рф/.su — WHOIS whois.tcinet.ru, поле 'paid-till'."""
    query = _to_ascii(domain)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(_RU_WHOIS, 43), timeout
        )
        writer.write((query + "\r\n").encode("ascii", "ignore"))
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(65536), timeout)
    except (asyncio.TimeoutError, OSError) as exc:
        return None, humanize_error(str(exc) or type(exc).__name__)
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass
    text = raw.decode("utf-8", "replace")
    for line in text.splitlines():
        if line.lower().startswith("paid-till:"):
            dt = _parse_date(line.split(":", 1)[1])
            return (_days_until(dt), "ok") if dt else (None, "не разобрать дату")
    if "no entries found" in text.lower() or "not found" in text.lower():
        return None, "домен не зарегистрирован"
    return None, "нет данных WHOIS"


async def _domain_days(domain: str, timeout: float) -> tuple[int | None, str]:
    """Срок регистрации домена. RDAP по умолчанию; для российских зон (нет RDAP) —
    WHOIS-регистратуры. None = данных нет — не повод для алерта."""
    if not domain or "." not in domain:
        return None, "некорректный домен"
    if domain.endswith(_RU_ZONES):
        return await _domain_days_whois_ru(domain, timeout)
    return await _domain_days_rdap(domain, timeout)


# --- бережный доступ к регистратурам: кэш по registrable-домену + серийная
# очередь. Сотни мониторов на поддоменах одного домена = ОДИН RDAP/WHOIS-запрос;
# между реальными запросами пауза, чтобы rdap.org/whois не отвечали 429.
_DOMAIN_CACHE: dict[str, tuple[int | None, str, float]] = {}  # domain → (days, msg, ts)
_DOMAIN_TTL_OK = 6 * 3600.0   # успешный ответ живёт 6ч (срок меняется медленно)
_DOMAIN_TTL_ERR = 15 * 60.0   # ошибку (429/таймаут) переспрашиваем через 15 мин
# Пока до истечения далеко, 6 часов — правильная экономия запросов к регистратуре.
# Но когда домен на грани или уже просрочен, всё наоборот: его вот-вот продлят, и
# держать «истёк» ещё шесть часов после оплаты — врать в лицо. Живой случай:
# домен продлили в 12:38, реестр знал об этом сразу, панель показывала «истёк»
# до вечера. В этом состоянии переспрашиваем раз в полчаса.
_DOMAIN_TTL_HOT = 30 * 60.0
_DOMAIN_HOT_DAYS = 7  # «на грани»: осталось ≤7 дней либо уже просрочен
_domain_gate = asyncio.Lock()  # регистратуры опрашиваем строго по одному
_DOMAIN_GAP_S = 1.1            # межзапросная пауза
_domain_last_at = 0.0


def _domain_cache_get(domain: str) -> tuple[int | None, str] | None:
    hit = _DOMAIN_CACHE.get(domain)
    if hit is None:
        return None
    days, msg, ts = hit
    if days is None:
        ttl = _DOMAIN_TTL_ERR
    elif days <= _DOMAIN_HOT_DAYS:
        ttl = _DOMAIN_TTL_HOT
    else:
        ttl = _DOMAIN_TTL_OK
    if time.monotonic() - ts >= ttl:
        return None
    return days, msg


def forget_domain(target: str) -> None:
    """Забыть кэш срока для домена (ручной запуск монитора).

    Человек жмёт «Проверить сейчас» именно тогда, когда он что-то ПОМЕНЯЛ:
    оплатил домен, перевыпустил сертификат. Отвечать ему из кэша — ровно то,
    чего он не хотел."""
    host = (target or "").strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/")[0].split(":")[0].strip(".")
    for key in list(_DOMAIN_CACHE):
        # ключ кэша — registrable-домен, а монитор может стоять на поддомене
        if host == key or host.endswith("." + key):
            _DOMAIN_CACHE.pop(key, None)


async def _domain_days_cached(domain: str, timeout: float) -> tuple[int | None, str]:
    cached = _domain_cache_get(domain)
    if cached is not None:
        return cached
    global _domain_last_at
    async with _domain_gate:
        # пока ждали очередь, домен мог уже спросить другой монитор
        cached = _domain_cache_get(domain)
        if cached is not None:
            return cached
        wait = _DOMAIN_GAP_S - (time.monotonic() - _domain_last_at)
        if wait > 0:
            await asyncio.sleep(wait)
        days, msg = await _domain_days(domain, timeout)
        _domain_last_at = time.monotonic()
        _DOMAIN_CACHE[domain] = (days, msg, _domain_last_at)
        return days, msg


@dataclass
class ExpiryInfo:
    ssl_days: int | None = None
    ssl_message: str = ""
    domain_days: int | None = None
    domain_message: str = ""


async def probe_expiry(check) -> ExpiryInfo:
    """Медленные сигналы http-монитора: срок TLS-сертификата и срок регистрации домена.
    Считаются реже основной проверки; не влияют на up/degraded/down."""
    info = ExpiryInfo()
    url = check.target.strip()
    parsed = urlparse(url if "://" in url else "https://" + url)
    host = parsed.hostname or ""
    timeout = max(check.timeout_ms, 1000) / 1000.0
    if getattr(check, "check_ssl", False) and host:
        if parsed.scheme == "http":
            info.ssl_message = "не HTTPS"
        else:
            days, msg = await _ssl_probe(host, parsed.port or 443, timeout)
            info.ssl_days = None if days is None else int(days)
            info.ssl_message = msg
    if getattr(check, "check_domain", False):
        days, msg = await _domain_days_cached(_registrable_domain(url), max(timeout, 10.0))
        info.domain_days = days
        info.domain_message = msg
    return info


def _parse_headers(raw: str) -> dict[str, str]:
    """Кастомные HTTP-заголовки монитора из JSON-текста (напр. {"x-token":"…"}).
    Толерантно: пусто/невалидный JSON/не-объект → {} (не роняем проверку)."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def eval_parts(check, code: int, kw_up_ok: bool, kw_down_found: bool,
               latency: int, degraded_ms: int) -> CheckOutcome:
    """Вердикт по «разобранному» ответу: код, попадание ключевых слов, задержка.

    Отдельно от _eval_http, потому что источник ответа бывает разный: панель держит
    в руках сам ответ, а от агента приходят только факты (тело закрытой страницы
    наружу не отдаём). Правила должны остаться ОДНИ, иначе один и тот же сайт
    получал бы разный вердикт в зависимости от того, кто его проверил."""
    if not status_matches(code, check.expected_status):
        return CheckOutcome("down", latency, message=http_status_text(code))
    if check.keyword_up and not kw_up_ok:
        return CheckOutcome("down", latency, message=f"нет ключевого слова «{check.keyword_up}»")
    if check.keyword_down and kw_down_found:
        return CheckOutcome("down", latency, message=f"найдено запрещённое «{check.keyword_down}»")
    if latency > degraded_ms:
        return CheckOutcome("degraded", latency, message=f"медленно: {latency} мс")
    return CheckOutcome("up", latency, message=f"HTTP {code} · {latency} мс")


# Сколько интервалов монитора можно не получать результат от агента, прежде чем
# считать это отказом. Два: один пропуск бывает от совпадения расписаний (агент
# отчитывается своим тактом), а вот два подряд — это уже молчание.
AGENT_PROBE_STALE_INTERVALS = 2


def outcome_from_agent(check, probe, now, degraded_ms: int) -> CheckOutcome:
    """Вердикт по результату, присланному агентом с самого сервера."""
    if probe is None:
        return CheckOutcome("down", message="агент ещё не присылал результат проверки")
    ts = probe.ts if probe.ts.tzinfo else probe.ts.replace(tzinfo=timezone.utc)
    age = (now - ts).total_seconds()
    limit = max(check.interval_seconds, 30) * AGENT_PROBE_STALE_INTERVALS
    if age > limit:
        mins = int(age // 60)
        return CheckOutcome(
            "down",
            message=f"агент не присылает результат {mins} мин — проверка с сервера не идёт",
        )
    if probe.error:
        # Обрыв на локальной проверке почти всегда означает одно: белый список сайта
        # не пускает 127.0.0.1. Снаружи такой сайт закрыт намеренно, а изнутри —
        # по недосмотру, и без подсказки это выглядит как «сайт лежит».
        low = probe.error.lower()
        if "eof" in low or "reset by peer" in low or "connection refused" in low:
            return CheckOutcome(
                "down", probe.latency_ms,
                message="сервер оборвал соединение изнутри — вероятно, localhost "
                        f"не разрешён в белом списке сайта ({probe.error[:80]})",
            )
        # Остальное прогоняем через тот же словарь подсказок, что и свои ошибки:
        # «connection refused» инженеру понятно, дежурному — нет, а читают одни люди.
        return CheckOutcome("down", probe.latency_ms, message=humanize_error(probe.error))
    return eval_parts(check, probe.code, probe.kw_up_found, probe.kw_down_found,
                      probe.latency_ms or 0, degraded_ms)


def _eval_http(check, r, body: str, latency: int, fell_back: bool, degraded_ms: int) -> CheckOutcome:
    """Оценивает ответ http-проверки → CheckOutcome (общая логика для одиночной
    проверки и для проверки каждого IP в режиме «все адреса»)."""
    if not status_matches(r.status_code, check.expected_status):
        return CheckOutcome("down", latency, message=http_status_text(r.status_code))
    if check.keyword_up and check.keyword_up not in body:
        return CheckOutcome("down", latency, message=f"нет ключевого слова «{check.keyword_up}»")
    if check.keyword_down and check.keyword_down in body:
        return CheckOutcome("down", latency, message=f"найдено запрещённое «{check.keyword_down}»")
    if fell_back:
        return CheckOutcome(
            "degraded", latency,
            message=f"⚠ HTTPS недоступен, отвечает только HTTP · {latency} мс",
        )
    if latency > degraded_ms:
        return CheckOutcome("degraded", latency, message=f"медленно: {latency} мс")
    return CheckOutcome("up", latency, message=f"HTTP {r.status_code} · {latency} мс")


async def _resolve_ips(host: str, port: int) -> list[str]:
    """IPv4-адреса домена (для проверки «все адреса»). Только A-записи: если
    резолвить и AAAA, а у хоста панели нет IPv6-маршрута — получим ложные «down»."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port or None, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
    except OSError:
        return []
    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    return ips


_STATUS_ORDER = {"down": 0, "degraded": 1, "up": 2}


async def _run_http_all_ips(check, url: str, method: str, auth, timeout, degraded_ms, attempt):
    """Проверяет КАЖДЫЙ IP домена (Host+SNI = домен, TLS проверяется по домену).
    Возвращает агрегат: худший статус побеждает; None → у домена ≤1 адрес (тогда
    вызывающий делает обычную одиночную проверку). Ловит мёртвый бэкенд за LB."""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return None
    port = parsed.port
    ips = await _resolve_ips(host, port or (443 if parsed.scheme == "https" else 80))
    if len(ips) <= 1:
        return None  # один адрес (или не зарезолвилось) → обычная проверка
    host_hdr = parsed.netloc  # host[:port] оригинала — в заголовок Host
    results: list[tuple[str, CheckOutcome]] = []
    for ip in ips:
        netloc = f"[{ip}]" if ":" in ip else ip
        if port:
            netloc += f":{port}"
        ip_url = parsed._replace(netloc=netloc).geturl()
        t0 = time.perf_counter()
        try:
            r, body = await attempt(
                ip_url, extra_headers={"Host": host_hdr},
                extensions={"sni_hostname": host},
            )
        except httpx.HTTPError as exc:
            results.append((ip, CheckOutcome("down", message=humanize_error(str(exc) or type(exc).__name__))))
            continue
        latency = int((time.perf_counter() - t0) * 1000)
        results.append((ip, _eval_http(check, r, body, latency, False, degraded_ms)))

    total = len(results)
    n_up = sum(1 for _, o in results if o.status == "up")
    worst = min(results, key=lambda kv: _STATUS_ORDER[kv[1].status])[1]
    # разбивка по IP — для показа в детали монитора (какой адрес что отдал)
    breakdown = [
        {"ip": ip, "status": o.status, "latency_ms": o.latency_ms, "message": o.message}
        for ip, o in results
    ]
    if worst.status == "up":
        lat = max((o.latency_ms or 0) for _, o in results)
        return CheckOutcome("up", lat, message=f"все {total} адреса OK · {lat} мс", ip_results=breakdown)
    bad = [f"{ip} — {o.message}" for ip, o in results if o.status != "up"]
    return CheckOutcome(
        worst.status, worst.latency_ms,
        message=_short(f"{n_up}/{total} адресов OK; " + "; ".join(bad[:4])),
        ip_results=breakdown,
    )


async def _run_http(
    check, proxy: str | None = None, degraded_ms: int | None = None,
    timeout_ms: int | None = None,
) -> CheckOutcome:
    raw = check.target.strip()
    # схему не указали → приоритет https, с авто-фолбэком на http если https не поднялся
    auto_scheme = not urlparse(raw).scheme
    url = ("https://" + raw) if auto_scheme else raw
    timeout = max(timeout_ms or check.timeout_ms, 1000) / 1000.0
    degraded_ms = check.degraded_ms if degraded_ms is None else degraded_ms
    # если заданы ключевые слова — нужен GET (у HEAD нет тела)
    method = "GET" if (check.keyword_up or check.keyword_down) else (check.method or "GET")
    # HTTP Basic auth для сайтов за 401 (логин/пароль заданы в мониторе)
    auth = (
        httpx.BasicAuth(getattr(check, "auth_user", "") or "", getattr(check, "auth_pass", "") or "")
        if getattr(check, "auth_method", "") == "basic"
        else None
    )
    # кастомные заголовки монитора (напр. x-application-token для сайтов за 401)
    custom_headers = _parse_headers(getattr(check, "http_headers", ""))
    verify = not getattr(check, "ignore_tls", False)  # игнор TLS-ошибок (самоподпис./mismatch)

    async def attempt(u: str, extra_headers: dict | None = None, extensions: dict | None = None):
        headers = {"User-Agent": _USER_AGENT, **custom_headers}
        if extra_headers:
            headers.update(extra_headers)
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, proxy=proxy,
            headers=headers, auth=auth, verify=verify,
        ) as client:
            resp = await client.request(method, u, extensions=extensions or {})
            text = resp.text[:_MAX_BODY] if (check.keyword_up or check.keyword_down) else ""
            return resp, text

    # опция «проверять все адреса домена» (по умолчанию выкл.) — только для прямых
    # проверок; через прокси адреса резолвит сам прокси, пинить IP смысла нет
    if getattr(check, "check_all_ips", False) and proxy is None:
        multi = await _run_http_all_ips(check, url, method, auth, timeout, degraded_ms, attempt)
        if multi is not None:
            return multi
        # ≤1 адрес → падаем в обычную одиночную проверку ниже

    # HTTPS не поднялся, а схему подставили сами и пришлось откатиться на http —
    # это не «всё ок»: сайт задумывался как https, но по факту доступен только http.
    fell_back = False
    t0 = time.perf_counter()
    try:
        r, body = await attempt(url)
    except httpx.HTTPError as exc:
        # любой сбой https (не подключился / SSL / оборвал соединение / таймаут) —
        # и схему подставили сами → пробуем http. Поднялся → «только HTTP» (degraded).
        if auto_scheme and url.startswith("https://"):
            try:
                r, body = await attempt("http://" + raw)
                fell_back = True
            except httpx.HTTPError as exc2:
                return CheckOutcome(
                    "down", message=_short(str(exc2) or type(exc2).__name__)
                )
        else:
            return CheckOutcome("down", message=humanize_error(str(exc) or type(exc).__name__))
    latency = int((time.perf_counter() - t0) * 1000)
    return _eval_http(check, r, body, latency, fell_back, degraded_ms)


async def _run_tcp_port(check) -> CheckOutcome:
    host = check.target.strip()
    port = check.port or 80
    timeout = max(check.timeout_ms, 1000) / 1000.0
    t0 = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 — закрытие не критично
            pass
    except (asyncio.TimeoutError, OSError) as exc:
        return CheckOutcome("down", message=_short(f"порт {port}: {exc or 'недоступен'}"))
    latency = int((time.perf_counter() - t0) * 1000)
    if latency > check.degraded_ms:
        return CheckOutcome("degraded", latency, message=f"порт {port} открыт, медленно: {latency} мс")
    return CheckOutcome("up", latency, message=f"порт {port} открыт · {latency} мс")


async def _run_cert(check) -> CheckOutcome:
    host = check.target.strip()
    # target может быть URL — вытащим host
    if "://" in host:
        host = urlparse(host).hostname or host
    port = check.port or 443
    timeout = max(check.timeout_ms, 1000) / 1000.0
    days, msg = await _ssl_probe(host, port, timeout)
    if days is None:
        return CheckOutcome("down", message=_short(f"сертификат {msg}"))
    rdays = round(days, 1)
    warn = max(check.ssl_warn_days) if check.ssl_warn_days else 14
    if days <= 0:
        return CheckOutcome("down", value=rdays, message="сертификат истёк")
    if days < warn:
        return CheckOutcome("degraded", value=rdays, message=f"сертификат истекает через {int(days)} дн.")
    return CheckOutcome("up", value=rdays, message=f"сертификат: {int(days)} дн. до истечения")


_RUNNERS = {"http": _run_http, "tcp_port": _run_tcp_port, "cert": _run_cert}


async def _run_once(check) -> CheckOutcome:
    runner = _RUNNERS.get(check.type)
    if runner is None:
        return CheckOutcome("down", message=f"неизвестный тип: {check.type}")
    try:
        return await runner(check)
    except Exception as exc:  # noqa: BLE001 — не роняем планировщик
        return CheckOutcome("down", message=humanize_error(str(exc) or type(exc).__name__))


async def probe_via_proxy(check, proxy_url: str) -> CheckOutcome:
    """Http-проверка сайта через локацию. Пустой proxy_url = напрямую (без прокси).
    Через прокси проверка СМЯГЧЕНА: даль + оверхед прокси делают её медленнее и
    флапнее, поэтому даём (а) больший таймаут, (б) повышенный порог «медленно» и
    (в) внутренние повторы перед «недоступен» — чтобы далёкий, но живой сайт (напр.
    бразильский через KZ-прокси) не мигал ложным down."""
    if not proxy_url:  # прямая проверка — без послаблений
        try:
            return await _run_http(check)
        except Exception as exc:  # noqa: BLE001
            return CheckOutcome("down", message=humanize_error(str(exc) or type(exc).__name__))

    s = get_settings()
    degraded = check.degraded_ms + max(s.location_degraded_extra_ms, 0)
    timeout_ms = check.timeout_ms + max(s.location_timeout_extra_ms, 0)  # запас на даль/прокси
    attempts = 1 + max(s.location_retries, 0)
    delay = max(s.check_retry_delay_ms, 0) / 1000.0
    outcome = CheckOutcome("down", message="проба не выполнилась")
    for i in range(attempts):
        try:
            outcome = await _run_http(
                check, proxy=proxy_url, degraded_ms=degraded, timeout_ms=timeout_ms
            )
        except Exception as exc:  # noqa: BLE001
            outcome = CheckOutcome("down", message=humanize_error(str(exc) or type(exc).__name__))
        if outcome.status in ("up", "degraded"):
            return outcome  # живой (пусть и медленный) — не ретраим
        if i + 1 < attempts:
            await asyncio.sleep(delay)
    return outcome


# нейтральные лёгкие эндпоинты для проверки, что через прокси идёт трафик
# (отдают 204/маленькое тело); успех на любом = прокси работает
_PROXY_TEST_TARGETS = (
    "https://www.gstatic.com/generate_204",
    "https://cloudflare.com/cdn-cgi/trace",
    "https://www.google.com/generate_204",
)


async def test_proxy(proxy_url: str, timeout_s: float = 10.0) -> tuple[bool, str, int | None]:
    """Проверяет доступность прокси: делает запрос к нейтральному эндпоинту ЧЕРЕЗ
    него. Любой HTTP-ответ = прокси принимает и форвардит трафик (ok). Исключение
    на всех целях = прокси недоступен/битый. Пустой url = «напрямую» → ok."""
    if not proxy_url.strip():
        return True, "напрямую (без прокси)", None
    last = ""
    for target in _PROXY_TEST_TARGETS:
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=timeout_s, proxy=proxy_url.strip(),
                headers={"User-Agent": _USER_AGENT}, follow_redirects=True,
            ) as client:
                r = await client.get(target)
            lat = int((time.perf_counter() - t0) * 1000)
            return True, f"прокси работает · HTTP {r.status_code} · {lat} мс", lat
        except Exception as exc:  # noqa: BLE001 — пробуем следующую цель
            last = humanize_error(str(exc) or type(exc).__name__)
    return False, last or "прокси недоступен", None


async def run_check(check) -> CheckOutcome:
    """Исполняет монитор с повторами при неуспехе: до (1 + retries) попыток.
    Возвращает первый успех (up); если успеха не было — исход последней попытки.
    Так одиночный сетевой блип не приводит к ложному инциденту/алерту."""
    attempts = 1 + max(getattr(check, "retries", 0) or 0, 0)
    delay = max(get_settings().check_retry_delay_ms, 0) / 1000.0
    outcome = await _run_once(check)
    for _ in range(1, attempts):
        if outcome.status == "up":
            return outcome
        await asyncio.sleep(delay)
        outcome = await _run_once(check)
    return outcome


def _short(s: str) -> str:
    return s[:500]


# Что панель ловит на практике и что это значит для человека. Ключ ищем в тексте
# исключения (без учёта регистра); первое совпадение выигрывает, поэтому более
# частные строки идут раньше общих.
#
# «[Errno -2] Name or service not known» инженеру ещё понятно, дежурному — нет,
# а в алерте это первое, что он читает. Техническую суть не выбрасываем: она
# остаётся в скобках, чтобы по сообщению можно было гуглить и сверять с логом.
_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    # --- DNS
    ("name or service not known", "домен не находится в DNS"),
    ("nodename nor servname provided", "домен не находится в DNS"),
    ("temporary failure in name resolution", "DNS временно не отвечает"),
    ("no address associated with hostname", "у домена нет IP-адреса"),
    # --- соединение
    ("connection refused", "сервер отклонил соединение — порт закрыт"),
    ("all connection attempts failed", "не удалось подключиться ни по одному адресу"),
    ("network is unreachable", "сеть недоступна с этой точки проверки"),
    ("no route to host", "нет маршрута до сервера"),
    ("connection reset by peer", "сервер оборвал соединение"),
    ("server disconnected", "сервер разорвал соединение, не ответив"),
    ("connecttimeout", "сервер не ответил на попытку соединения"),
    ("readtimeout", "сервер принял запрос, но не прислал ответ вовремя"),
    ("writetimeout", "не удалось отправить запрос — сервер не принимал данные"),
    ("pooltimeout", "не дождались свободного соединения"),
    ("timeout", "истекло время ожидания"),
    # --- TLS
    ("certificate has expired", "TLS-сертификат истёк"),
    ("certificate verify failed", "TLS-сертификат не прошёл проверку"),
    ("self signed certificate", "TLS-сертификат самоподписанный"),
    ("hostname mismatch", "TLS-сертификат выписан на другое имя"),
    ("doesn't match", "TLS-сертификат выписан на другое имя"),
    ("sslv3", "ошибка TLS-рукопожатия"),
    ("wrong version number", "на порту не TLS — возможно, обычный HTTP"),
    ("ssl", "ошибка TLS"),
    # --- протокол
    ("too many redirects", "слишком много перенаправлений"),
    ("invalid url", "адрес записан неверно"),
    ("proxy", "точка проверки не смогла пройти через прокси"),
)


# Что означает код ответа — словами. Берём только то, что реально встречается у
# мониторинга сайтов; остальное покажем как есть («HTTP 418» без пересказа).
_HTTP_REASON: dict[int, str] = {
    400: "сервер не понял запрос",
    401: "нужна авторизация",
    402: "требуется оплата",
    403: "доступ запрещён",
    404: "страница не найдена",
    405: "метод запроса не разрешён",
    407: "прокси требует авторизации",
    408: "сервер не дождался запроса",
    409: "конфликт запроса",
    410: "страница удалена навсегда",
    413: "запрос слишком большой",
    418: "я чайник",
    421: "запрос пришёл не на тот сервер",
    422: "сервер не смог обработать данные",
    429: "слишком много запросов — нас ограничивают",
    431: "заголовки запроса слишком велики",
    451: "закрыто по требованию правообладателя или закона",
    500: "внутренняя ошибка сервера",
    501: "сервер не умеет такой запрос",
    502: "плохой ответ от бэкенда",
    503: "сервис недоступен — перегружен или на обслуживании",
    504: "бэкенд не ответил вовремя",
    505: "версия HTTP не поддерживается",
    507: "на сервере кончилось место",
    508: "сервер зациклился",
    # Cloudflare и подобные прокси: коды нестандартные, но встречаются постоянно
    520: "прокси получил непонятный ответ от сервера",
    521: "сервер за прокси не отвечает",
    522: "прокси не смог подключиться к серверу",
    523: "прокси не видит сервер",
    524: "сервер за прокси не ответил вовремя",
    525: "не удалось установить TLS между прокси и сервером",
    526: "у сервера за прокси недействительный сертификат",
    530: "ошибка на стороне прокси",
}


def http_status_text(code: int) -> str:
    """«403» → «доступ запрещён (HTTP 403)». Незнакомый код — просто «HTTP 418»."""
    reason = _HTTP_REASON.get(code)
    return f"{reason} (HTTP {code})" if reason else f"HTTP {code}"


def humanize_error(text: str) -> str:
    """«[Errno -2] Name or service not known» → «домен не находится в DNS (…)».

    Неизвестную ошибку не выдумываем — отдаём как есть: лучше непонятный текст,
    чем уверенное враньё о причине."""
    raw = (text or "").strip()
    low = raw.lower()
    for needle, human in _ERROR_HINTS:
        if needle in low:
            # скобка с оригиналом нужна для диагностики, но не должна распухать
            return _short(f"{human} ({raw[:120]})")
    return _short(raw)
