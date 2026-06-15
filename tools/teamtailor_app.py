"""Кликабельный просмотр + подача Teamtailor (изолированно от Salling/7-Eleven).

Показывает вакансии из коннектора Teamtailor и у каждой — кнопку «Заполнить
заявку». По клику открывается настоящее окно браузера на форме подачи, бот
заполняет известные поля и CV и ОСТАНАВЛИВАЕТСЯ — согласие и «Отправить» жмёшь
сам. Ничего не отправляется автоматически.

Запуск:  python tools/teamtailor_app.py        (откроется на http://127.0.0.1:8078)
"""
import html
import json
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import connectors  # noqa: E402
from connectors.apply_dispatch import detect, platform_name  # noqa: E402

PORT = 8078
_JOBS = []           # кэш вакансий (source-of-truth для страницы и подачи)
_LOCK = threading.Lock()

_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def refresh_jobs():
    jobs = []
    for conn in connectors.all_connectors():
        try:
            got = conn.search()
            print(f"  {conn.name}: {len(got)}")
            jobs.extend(got)
        except Exception as e:
            print(f"  {conn.name}: ошибка {e}")
    jobs.sort(key=lambda j: (j.source, j.company, j.title))
    with _LOCK:
        _JOBS[:] = jobs
    print(f"  загружено вакансий всего: {len(jobs)}")


def launch_filler(job_url: str):
    """Открыть видимое окно браузера на форме подачи (отдельный процесс).
    Через диспетчер — работает для любой поддерживаемой платформы."""
    cmd = [sys.executable, "-m", "connectors.apply_dispatch", job_url, "--keep-open"]
    kw = {"cwd": str(ROOT)}
    if sys.platform == "win32":
        kw["creationflags"] = _DETACHED
    subprocess.Popen(cmd, **kw)


def _loading_html() -> str:
    return ("<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='3'>"
            "<title>WexFlow — подача (БЕТА)</title>"
            "<body style='margin:0;font-family:Segoe UI,sans-serif;background:#0d0e0e;color:#e8eae8'>"
            "<div style='max-width:700px;margin:18vh auto;text-align:center'>"
            "<h1 style='color:#1ed760'>WexFlow — подача <span style='font-size:14px;"
            "border:1px solid #6b66ff;color:#6b66ff;border-radius:999px;padding:2px 8px'>БЕТА</span></h1>"
            "<p style='color:#8b908c'>Загружаю датские вакансии с платформ… (несколько секунд)</p></div></body>")


CARD_CAP = 200  # столько карточек показываем (1000+ тормозят браузер); подача — по ссылке

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
  .wrap{{max-width:1100px;margin:0 auto;padding:36px 24px 80px}}
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

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, page_html())
        else:
            self._send(404, "not found")

    def do_POST(self):
        if self.path not in ("/apply", "/apply-url"):
            self._send(404, "not found")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/apply":
                with _LOCK:
                    job = _JOBS[int(body["i"])]
                url = job.url
            else:
                url = (body.get("url") or "").strip()
                if not url:
                    raise ValueError("пустая ссылка")
            key = detect(url)
            launch_filler(url)
            self._send(200, json.dumps(
                {"ok": True, "platform": platform_name(key) if key else "универсально"}
            ), "application/json")
        except Exception as e:
            self._send(200, json.dumps({"ok": False, "error": str(e)}), "application/json")


def main():
    url = f"http://127.0.0.1:{PORT}"
    # вакансии грузим в фоне — сервер поднимается мгновенно, страница покажет «загрузка»
    threading.Thread(target=refresh_jobs, daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # порт занят — вероятно бета уже запущена; просто открываем её
        webbrowser.open(url)
        return
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"WexFlow — подача (БЕТА): {url}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
