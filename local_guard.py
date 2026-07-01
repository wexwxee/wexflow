"""Единый страж локальных запросов: только loopback + защита от кросс-сайта.

Локальные серверы WexFlow (Salling app.py, hub.py, модуль подачи connectors/
webapp.py) отвечают только на 127.0.0.1. Но браузер со стороннего сайта всё
равно может послать POST на localhost («CSRF»), поэтому пишущие запросы
пропускаем, лишь если Host и Origin/Referer — тоже loopback.

Раньше этот барьер был СКОПИРОВАН в три файла (F41). Дубликат защитного кода
опасен: поправишь в одном месте — забудешь в другом. Здесь единственный
источник правды. Функции берут «сырые» строки заголовков, поэтому годятся и
для FastAPI (Request.headers), и для http.server (self.headers).
"""
from urllib.parse import urlsplit

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_CROSS_SITE = {"cross-site", "same-site"}


def is_loopback_host(host: str) -> bool:
    """True, если Host-заголовок указывает на локальную петлю (с портом или без)."""
    host = (host or "").strip().lower()
    if host.startswith("[") and "]" in host:      # [::1]:port
        host = host[1:host.index("]")]
    else:
        host = host.split(":", 1)[0]
    return host in _LOOPBACK_HOSTS


def is_loopback_url(value: str) -> bool:
    """True, если Origin/Referer-URL ведёт на локальную петлю."""
    try:
        return is_loopback_host(urlsplit(value).hostname or "")
    except Exception:  # noqa: BLE001 — битый URL считаем не-loopback
        return False


def cross_site_allowed(host: str, origin: str, referer: str, sec_fetch_site: str = "") -> bool:
    """Разрешён ли пишущий запрос по заголовкам (метод уже признан «пишущим»).

    Host обязан быть loopback; при наличии Origin/Referer — они тоже loopback;
    иначе смотрим Sec-Fetch-Site (cross-site/same-site → запрет)."""
    if not is_loopback_host(host):
        return False
    if origin:
        return is_loopback_url(origin)
    if referer:
        return is_loopback_url(referer)
    return (sec_fetch_site or "").lower() not in _CROSS_SITE


def allowed_write(method: str, host: str, origin: str, referer: str,
                  sec_fetch_site: str = "") -> bool:
    """Блокирует кросс-сайтовые запись-запросы к локальному серверу.

    Безопасные методы (GET/HEAD/OPTIONS…) — всегда True. Для POST/PUT/PATCH/
    DELETE требуется loopback-Host и loopback-Origin/Referer."""
    if (method or "").upper() not in UNSAFE_METHODS:
        return True
    return cross_site_allowed(host, origin, referer, sec_fetch_site)
