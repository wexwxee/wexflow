"""Просмотр Этапа 1: тянет вакансии Teamtailor через коннектор и пишет
самодостаточную HTML-страницу (можно открыть двойным кликом).

Запуск:  python tools/teamtailor_preview.py
Результат: design_drafts/teamtailor_preview.html
"""
import html
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import connectors  # noqa: E402

OUT = ROOT / "design_drafts" / "teamtailor_preview.html"


def _city(j) -> str:
    return j.city or "—"


def main() -> int:
    tt = connectors.get("teamtailor")
    jobs = tt.search()
    jobs.sort(key=lambda j: (j.company, j.title))
    companies = sorted({j.company for j in jobs})
    by_city = Counter(_city(j) for j in jobs)

    cards = []
    for j in jobs:
        loc = " · ".join(x for x in [j.city, j.zip] if x) or "локация не указана"
        date = (j.published or "")[:10]
        cards.append(f"""
        <a class="card" href="{html.escape(j.url)}" target="_blank" rel="noopener">
          <div class="card-top">
            <span class="company">{html.escape(j.company)}</span>
            <span class="date">{html.escape(date)}</span>
          </div>
          <div class="title">{html.escape(j.title)}</div>
          <div class="loc">📍 {html.escape(loc)}</div>
        </a>""")

    top_cities = " · ".join(f"{c} ({n})" for c, n in by_city.most_common(6))

    page = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Teamtailor — просмотр (Этап 1)</title>
<style>
  :root {{
    --bg:#0d0e0e; --bg2:#121313; --card:#1c1d1d; --line:#2b2c2c;
    --txt:#e8eae8; --muted:#8b908c; --neon:#1ed760; --tt:#6b66ff;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:'Segoe UI',system-ui,sans-serif; color:var(--txt);
         background:radial-gradient(1200px 600px at 80% -10%, #16221b 0%, var(--bg) 55%); }}
  .wrap {{ max-width:1100px; margin:0 auto; padding:40px 24px 80px; }}
  .head {{ display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; }}
  h1 {{ font-size:30px; margin:0; letter-spacing:-0.5px; }}
  .pill {{ font-size:12px; padding:3px 10px; border-radius:999px; background:#1a1b1b;
          border:1px solid var(--line); color:var(--muted); }}
  .tt {{ color:var(--tt); border-color:#2c2b55; }}
  .stats {{ margin:18px 0 6px; color:var(--muted); font-size:15px; line-height:1.6; }}
  .stats b {{ color:var(--neon); font-weight:600; }}
  .cities {{ color:var(--muted); font-size:13px; margin-bottom:26px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:14px; }}
  .card {{ display:block; text-decoration:none; color:inherit; background:var(--card);
          border:1px solid var(--line); border-radius:14px; padding:16px 18px;
          transition:transform .12s ease, border-color .12s ease; }}
  .card:hover {{ transform:translateY(-3px); border-color:#3a3d3a; }}
  .card-top {{ display:flex; justify-content:space-between; align-items:center; gap:10px; }}
  .company {{ font-size:12px; color:var(--tt); font-weight:600; text-transform:uppercase;
             letter-spacing:.4px; }}
  .date {{ font-size:11px; color:var(--muted); }}
  .title {{ font-size:16px; font-weight:600; margin:8px 0 10px; line-height:1.3; }}
  .loc {{ font-size:13px; color:var(--muted); }}
  .foot {{ margin-top:34px; color:var(--muted); font-size:12px; }}
</style></head>
<body><div class="wrap">
  <div class="head">
    <h1>Teamtailor — просмотр</h1>
    <span class="pill tt">Этап 1 · только просмотр</span>
  </div>
  <div class="stats">
    <b>{len(jobs)}</b> вакансий от <b>{len(companies)}</b> компаний —
    получено напрямую через публичный <code>jobs.json</code>, без скрейпинга.
  </div>
  <div class="cities">Города: {html.escape(top_cities)}</div>
  <div class="grid">{''.join(cards)}</div>
  <div class="foot">Источник: коннектор Teamtailor (connectors/teamtailor.py).
     Каталог компаний пополняется по одному. Подачи здесь нет — это Этап 2.</div>
</div></body></html>"""

    OUT.write_text(page, encoding="utf-8")
    print(f"Готово: {len(jobs)} вакансий от {len(companies)} компаний")
    print(f"Файл: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
