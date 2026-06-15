"""Единый адрес (reverse-proxy) для Salling + 7-Eleven.

Один порт 8080. Всё проксируется на активное приложение (cookie hub_active):
  - salling  -> http://127.0.0.1:8000
  - 7e       -> http://127.0.0.1:7111
Переключатель в шапке ведёт на /__app/salling или /__app/7eleven — это меняет
активное приложение и возвращает на «/». Пути приложений не переписываются:
пока активно одно приложение, ВСЕ его абсолютные пути (/job, /api, /static…)
идут именно ему. Так ничего не ломается.

Запуск:  python -m uvicorn hub:app --host 127.0.0.1 --port 8080
(оба бэкенда — Salling на 8000 и 7-Eleven на 7111 — должны быть подняты)
"""
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from urllib.parse import urlsplit

app = FastAPI(title="Job Apply Hub")

BACKENDS = {
    "salling": "http://127.0.0.1:8000",
    "7e": "http://127.0.0.1:7111",
}
_DROP_REQ = {"host", "content-length", "connection"}
_DROP_RESP = {"content-length", "content-encoding", "transfer-encoding", "connection"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _is_loopback_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if host.startswith("[") and "]" in host:
        host = host[1:host.index("]")]
    else:
        host = host.split(":", 1)[0]
    return host in {"127.0.0.1", "localhost", "::1"}


def _is_loopback_url(value: str) -> bool:
    try:
        return _is_loopback_host(urlsplit(value).hostname or "")
    except Exception:
        return False


def _allowed_local_write(request: Request) -> bool:
    if request.method.upper() not in UNSAFE_METHODS:
        return True
    if not _is_loopback_host(request.headers.get("host", "")):
        return False
    origin = request.headers.get("origin", "")
    if origin:
        return _is_loopback_url(origin)
    referer = request.headers.get("referer", "")
    if referer:
        return _is_loopback_url(referer)
    fetch_site = (request.headers.get("sec-fetch-site") or "").lower()
    if fetch_site in {"cross-site", "same-site"}:
        return False
    return True


@app.middleware("http")
async def _local_write_guard(request: Request, call_next):
    if not _allowed_local_write(request):
        return Response(
            content=b'{"ok":false,"error":"blocked cross-site request"}',
            status_code=403,
            media_type="application/json",
        )
    return await call_next(request)


@app.get("/__app/{which}")
def switch(which: str, next: str = "/"):
    active = "7e" if which.lower().startswith("7") else "salling"
    if not next.startswith("/") or next.startswith("//"):
        next = "/"
    resp = RedirectResponse(next, status_code=303)
    resp.set_cookie("hub_active", active, max_age=31536000, samesite="lax")
    return resp


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
async def proxy(path: str, request: Request):
    active = request.cookies.get("hub_active", "salling")
    base = BACKENDS.get(active, BACKENDS["salling"])
    url = f"{base}/{path}"
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQ}
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=120) as client:
            r = await client.request(
                request.method, url, params=request.query_params,
                content=body, headers=headers,
            )
    except Exception as e:
        which = "7-Eleven (порт 7111)" if active == "7e" else "Salling (порт 8000)"
        return Response(
            content=f"<h2>Сервис {which} недоступен.</h2><p>{e}</p>".encode("utf-8"),
            status_code=502, media_type="text/html; charset=utf-8",
        )
    resp_headers = [(k, v) for k, v in r.headers.items() if k.lower() not in _DROP_RESP]
    return Response(content=r.content, status_code=r.status_code, headers=dict(resp_headers))
