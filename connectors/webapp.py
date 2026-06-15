"""Веб-модуль «WexFlow — подача» (БЕТА): витрина датских вакансий + подача.

Внутри пакета connectors, чтобы попасть в сборку. Показывает вакансии всех
коннекторов и по клику открывает форму подачи (заполняет и ОСТАНАВЛИВАЕТСЯ —
согласие и «Отправить» жмёт человек). Запуск заполнителя:
- dev: отдельный python-процесс `-m connectors.apply_dispatch`;
- сборка (frozen): сам exe с воркер-аргументом `--worker-connector-apply`.
"""
from __future__ import annotations

import hashlib
import html
import json
import sys
import threading
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import connectors  # регистрирует коннекторы
from connectors.apply_dispatch import detect, platform_name

PORT = 8078
HUB_BACK = "http://127.0.0.1:8080/hub"
CARD_CAP = 200
_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

# --- простой парольный замок на бета-модуль ---
# Пароль для входа в бету. В куки кладём не сам пароль, а его хэш-токен.
BETA_PASSWORD = "123456"
_COOKIE_NAME = "wexbeta"
_AUTH_TOKEN = hashlib.sha256(("wexflow-beta-gate::" + BETA_PASSWORD).encode("utf-8")).hexdigest()
_COOKIE_MAX_AGE = 43200  # 12 часов — потом попросит пароль снова

_JOBS = []
_LOCK = threading.Lock()


def refresh_jobs():
    jobs = []
    for conn in connectors.all_connectors():
        try:
            jobs.extend(conn.search())
        except Exception as e:  # noqa: BLE001
            print(f"  {conn.name}: ошибка {e}")
    jobs.sort(key=lambda j: (j.source, j.company, j.title))
    with _LOCK:
        _JOBS[:] = jobs
    print(f"  бета: загружено вакансий {len(jobs)}")


def launch_filler(job_url: str):
    """Открыть видимое окно браузера на форме подачи отдельным процессом."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--worker-connector-apply", job_url]
    else:
        cmd = [sys.executable, "-m", "connectors.apply_dispatch", job_url, "--keep-open"]
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = _DETACHED
    import subprocess
    subprocess.Popen(cmd, **kw)


def _loading_html() -> str:
    return ("<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='3'>"
            "<title>WexFlow — подача (БЕТА)</title>"
            "<body style='margin:0;font-family:Segoe UI,sans-serif;background:#0d0e0e;color:#e8eae8'>"
            "<div style='max-width:700px;margin:18vh auto;text-align:center'>"
            "<h1 style='color:#1ed760'>WexFlow — подача <span style='font-size:14px;"
            "border:1px solid #6b66ff;color:#6b66ff;border-radius:999px;padding:2px 8px'>БЕТА</span></h1>"
            "<p style='color:#8b908c'>Загружаю датские вакансии с платформ… (несколько секунд)</p></div></body>")


def _login_html(error: bool = False) -> str:
    msg = ("<div style='color:#ff6b6b;font-size:12.5px;margin-top:10px'>"
           "Неверный пароль. Попробуй ещё раз.</div>" if error else "")
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WexFlow — подача (БЕТА)</title><style>
  *{{box-sizing:border-box}} body{{margin:0;font-family:'Segoe UI',system-ui,sans-serif;
    color:#e8eae8;min-height:100vh;display:grid;place-items:center;
    background:radial-gradient(1200px 600px at 80% -10%,#16221b,#0d0e0e 55%)}}
  .box{{width:min(360px,90vw);background:#1c1d1d;border:1px solid #2b2c2c;border-radius:16px;
    padding:30px 26px;text-align:center}}
  h1{{font-size:20px;margin:0 0 8px}}
  .tag{{font-size:12px;border:1px solid #6b66ff;color:#6b66ff;border-radius:999px;
    padding:2px 9px;vertical-align:middle}}
  p{{color:#8b908c;font-size:13px;margin:0 0 18px;line-height:1.4}}
  input{{width:100%;background:#141515;border:1px solid #2b2c2c;border-radius:10px;
    padding:12px 14px;color:#e8eae8;font:16px 'Segoe UI';text-align:center;letter-spacing:3px}}
  input:focus{{outline:none;border-color:#6b66ff}}
  button{{width:100%;margin-top:14px;border:0;border-radius:10px;padding:12px;
    font:600 15px 'Segoe UI';background:#6b66ff;color:#fff;cursor:pointer}}
  button:hover{{filter:brightness(1.08)}}
</style></head><body>
  <form class="box" method="post" action="/login">
    <h1>WexFlow — подача <span class="tag">БЕТА</span></h1>
    <p>Модуль защищён паролем. Введи пароль, чтобы открыть.</p>
    <input type="password" name="password" placeholder="Пароль" autofocus
           autocomplete="current-password" inputmode="numeric">
    {msg}
    <button type="submit">Открыть →</button>
  </form>
</body></html>"""


def page_html() -> str:
    with _LOCK:
        jobs = list(_JOBS)
    if not jobs:
        return _loading_html()
    shown = jobs[:CARD_CAP]
    cards = []
    for i, j in enumerate(shown):
        loc = " · ".join(x for x in [j.city, j.zip] if x) or "локация в заголовке"
        date = (j.published or "")[:10]
        cards.append(f"""
        <div class="card">
          <div class="card-top"><span class="company">{html.escape(j.company)} · {html.escape(j.source)}</span>
            <span class="date">{html.escape(date)}</span></div>
          <div class="title">{html.escape(j.title)}</div>
          <div class="loc">📍 {html.escape(loc)}</div>
          <button class="apply" data-i="{i}">Заполнить заявку →</button>
        </div>""")
    companies = len({j.company for j in jobs})
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WexFlow — подача</title><style>
  :root {{ --bg:#0d0e0e; --card:#1c1d1d; --line:#2b2c2c; --txt:#e8eae8;
    --muted:#8b908c; --neon:#1ed760; --tt:#6b66ff; }}
  *{{box-sizing:border-box}} body{{margin:0;font-family:'Segoe UI',system-ui,sans-serif;
    color:var(--txt);background:radial-gradient(1200px 600px at 80% -10%,#16221b,var(--bg) 55%)}}
  .wrap{{max-width:1100px;margin:0 auto;padding:24px 24px 80px}}
  .back{{display:inline-block;color:var(--muted);text-decoration:none;font-size:13px;margin-bottom:14px}}
  .back:hover{{color:var(--txt)}}
  h1{{font-size:28px;margin:0 0 4px}} .sub{{color:var(--muted);margin-bottom:24px}}
  .sub b{{color:var(--neon)}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}}
  .card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px}}
  .card-top{{display:flex;justify-content:space-between;gap:10px}}
  .company{{font-size:12px;color:var(--tt);font-weight:600;text-transform:uppercase;letter-spacing:.4px}}
  .date{{font-size:11px;color:var(--muted)}}
  .title{{font-size:16px;font-weight:600;margin:8px 0 8px;line-height:1.3}}
  .loc{{font-size:13px;color:var(--muted);margin-bottom:14px}}
  .apply{{width:100%;border:0;border-radius:10px;padding:10px;font:600 14px 'Segoe UI';
    background:var(--neon);color:#08210f;cursor:pointer}}
  .apply:hover{{filter:brightness(1.08)}} .apply:disabled{{opacity:.6;cursor:default}}
  .paste{{display:flex;gap:8px;margin:6px 0 26px}}
  .paste input{{flex:1;background:#141515;border:1px solid var(--line);border-radius:10px;
    padding:11px 14px;color:var(--txt);font:14px 'Segoe UI'}}
  .paste button{{border:0;border-radius:10px;padding:0 18px;font:600 14px 'Segoe UI';
    background:var(--tt);color:#fff;cursor:pointer}}
  .hint{{font-size:12px;color:var(--muted);margin:-18px 0 26px}}
  #toast{{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);background:#1c1d1d;
    border:1px solid var(--neon);color:var(--txt);padding:12px 18px;border-radius:10px;
    opacity:0;transition:.2s;pointer-events:none}} #toast.show{{opacity:1}}
</style></head><body><div class="wrap">
  <a class="back" href="{HUB_BACK}">← В WexFlow</a>
  <h1>WexFlow — подача <span style="font-size:14px;vertical-align:middle;border:1px solid var(--tt);color:var(--tt);border-radius:999px;padding:2px 9px">БЕТА</span></h1>
  <div class="sub">Заполняет форму и <b>останавливается</b> — согласие и «Отправить» жмёшь сам.
    Платформы: Teamtailor, Greenhouse, Ashby, Lever, Recruitee, Workable.</div>
  <div class="paste">
    <input id="link" type="text" placeholder="Вставь ссылку на вакансию (любая поддерживаемая платформа)…">
    <button id="go">Заполнить по ссылке →</button>
  </div>
  <div class="hint">Витрина: <b>{len(jobs)}</b> датских вакансий от <b>{companies}</b> компаний (Teamtailor + Greenhouse + Ashby), показаны первые <b>{len(shown)}</b>. Автозаполнение работает и для фирм, которых тут нет — по ссылке.</div>
  <div class="grid">{''.join(cards)}</div>
</div><div id="toast"></div><script>
  function toast(t){{var e=document.getElementById('toast');e.textContent=t;e.classList.add('show');
    setTimeout(function(){{e.classList.remove('show')}},3500);}}
  document.querySelectorAll('.apply').forEach(function(b){{
    b.addEventListener('click', function(){{
      b.disabled=true; var old=b.textContent; b.textContent='Открываю браузер…';
      fetch('/apply',{{method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{i:+b.dataset.i}})}})
        .then(r=>r.json()).then(d=>{{ toast(d.ok?'Окно открыто — проверь и отправь сам':'Ошибка: '+(d.error||''));
          setTimeout(function(){{b.disabled=false;b.textContent=old;}},2500); }})
        .catch(e=>{{toast('Ошибка запуска');b.disabled=false;b.textContent=old;}});
    }});
  }});
  var go=document.getElementById('go'), link=document.getElementById('link');
  function applyLink(){{
    var u=(link.value||'').trim(); if(!u){{toast('Вставь ссылку');return;}}
    go.disabled=true; var old=go.textContent; go.textContent='Открываю…';
    fetch('/apply-url',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{url:u}})}}).then(r=>r.json()).then(d=>{{
        toast(d.ok ? ('Платформа: '+d.platform+' — окно открыто, проверь и отправь сам')
                   : ('Ошибка: '+(d.error||'')));
        go.disabled=false; go.textContent=old;
      }}).catch(e=>{{toast('Ошибка запуска');go.disabled=false;go.textContent=old;}});
  }}
  go.addEventListener('click', applyLink);
  link.addEventListener('keydown', function(e){{ if(e.key==='Enter') applyLink(); }});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self) -> bool:
        """Есть ли в куках действительный токен входа в бету."""
        raw = self.headers.get("Cookie", "")
        if not raw:
            return False
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:  # noqa: BLE001
            return False
        m = jar.get(_COOKIE_NAME)
        return bool(m and m.value == _AUTH_TOKEN)

    def do_GET(self):
        if self.path.split("?", 1)[0] in ("/", "/index.html"):
            self._send(200, page_html() if self._authed() else _login_html())
        else:
            self._send(404, "not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/login":
            n = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            pw = (parse_qs(raw).get("password", [""])[0]).strip()
            if pw == BETA_PASSWORD:
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie",
                                 f"{_COOKIE_NAME}={_AUTH_TOKEN}; Path=/; "
                                 f"Max-Age={_COOKIE_MAX_AGE}; HttpOnly; SameSite=Lax")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send(200, _login_html(error=True))
            return
        if path not in ("/apply", "/apply-url"):
            self._send(404, "not found")
            return
        if not self._authed():
            self._send(200, json.dumps(
                {"ok": False, "error": "нужно войти — обнови страницу и введи пароль"}
            ), "application/json")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/apply":
                with _LOCK:
                    url = _JOBS[int(body["i"])].url
            else:
                url = (body.get("url") or "").strip()
                if not url:
                    raise ValueError("пустая ссылка")
            key = detect(url)
            launch_filler(url)
            self._send(200, json.dumps(
                {"ok": True, "platform": platform_name(key) if key else "универсально"}
            ), "application/json")
        except Exception as e:  # noqa: BLE001
            self._send(200, json.dumps({"ok": False, "error": str(e)}), "application/json")


def serve(port: int = PORT, open_browser: bool = False):
    url = f"http://127.0.0.1:{port}"
    threading.Thread(target=refresh_jobs, daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        if open_browser:
            webbrowser.open(url)
        return
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"WexFlow — подача (БЕТА): {url}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    p = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else PORT
    serve(p, open_browser=False)
