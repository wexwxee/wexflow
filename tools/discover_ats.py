"""Авто-разведка каталога ATS: перечисляет компании платформы через архив
интернета (Wayback CDX), проверяет каждую по публичному API и оставляет тех, у
кого есть ДАТСКИЕ вакансии. Найденных дописывает в каталог коннектора.

Запуск:  python tools/discover_ats.py greenhouse
         python tools/discover_ats.py ashby
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx
from connectors.base import is_denmark

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

H = {"User-Agent": "WexFlow/1.0 (+job-apply-hub)"}
TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}$")
JUNK = {"embed", "apps", "blog", "www", "api", "assets", "static", "well-known",
        "career", "careers", "eu", "au", "status", "go", "cdn", "partner",
        "support", "docs", "help", "analytics"}


def _tt_loc(job: dict) -> str:
    """Город+страна из Teamtailor jobs.json (для фильтра на Данию)."""
    jp = job.get("_jobposting") or {}
    locs = jp.get("jobLocation") or []
    if locs and isinstance(locs, list):
        a = (locs[0] or {}).get("address") or {}
        return f"{a.get('addressLocality','')} {a.get('addressCountry','')} {a.get('addressRegion','')}"
    return job.get("title", "")

# Платформа: как перечислять (Wayback), как проверять (API), как класть в каталог.
PLATFORMS = {
    "greenhouse": {
        "cdx": "boards.greenhouse.io/*",
        "token_re": r"boards\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)",
        "api": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
        "jobs_key": "jobs",
        "loc": lambda j: (j.get("location") or {}).get("name") or "",
        "catalog": ROOT / "connectors" / "greenhouse_companies.json",
        "id_field": "token",
    },
    "ashby": {
        "cdx": "jobs.ashbyhq.com/*",
        "token_re": r"jobs\.ashbyhq\.com/([\w-]+)",
        "api": "https://api.ashbyhq.com/posting-api/job-board/{t}",
        "jobs_key": "jobs",
        "loc": lambda j: j.get("location") or "",
        "catalog": ROOT / "connectors" / "ashby_companies.json",
        "id_field": "org",
    },
    "teamtailor": {
        "mode": "subdomain",
        "cdx_domain": "teamtailor.com",
        "api": "https://{t}.teamtailor.com/jobs.json",
        "jobs_key": "items",
        "loc": _tt_loc,
        "catalog": ROOT / "connectors" / "teamtailor_companies.json",
        "id_field": "slug",
    },
}


def _wayback(url: str) -> str:
    for _ in range(3):
        try:
            r = httpx.get(url, timeout=180, headers=H)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        time.sleep(4)
    return ""


def _clean(t: str) -> str | None:
    t = t.lower()
    if t.startswith("2f") or t.startswith("%"):   # артефакты кодирования URL
        return None
    t = re.sub(r"-\d{8,}$", "", t)                 # хвост-таймстамп Teamtailor
    if TOKEN_RE.match(t) and t not in JUNK and not t.isdigit():
        return t
    return None


def enumerate_tokens(cfg) -> set[str]:
    toks = set()
    if cfg.get("mode") == "subdomain":
        # компании = поддомены; берём из urlkey "com,domain,COMPANY)/..."
        # листаем весь алфавит через resumeKey (один запрос = ~начало алфавита).
        dom = cfg["cdx_domain"]
        base = dom.split(".")[0]
        resume = None
        for page in range(12):
            u = (f"http://web.archive.org/cdx/search/cdx?url={dom}&matchType=domain"
                 "&fl=urlkey&collapse=urlkey&limit=80000&showResumeKey=true&output=text")
            if resume:
                u += f"&resumeKey={quote(resume)}"
            txt = _wayback(u)
            if not txt:
                break
            lines = txt.splitlines()
            resume = None
            if len(lines) >= 2 and not lines[-2].strip() and lines[-1].strip():
                resume = lines[-1].strip()
                lines = lines[:-2]
            for line in lines:
                parts = line.split(")")[0].split(",")
                if len(parts) >= 3 and parts[1] == base:
                    c = _clean(parts[2])
                    if c:
                        toks.add(c)
            print(f"  …архив стр.{page+1}: всего кандидатов {len(toks)}")
            if not resume:
                break
    else:
        txt = _wayback(f"http://web.archive.org/cdx/search/cdx?url={cfg['cdx']}"
                       "&output=text&fl=original&collapse=urlkey&limit=50000")
        for line in txt.splitlines():
            m = re.search(cfg["token_re"], line)
            if m:
                c = _clean(m.group(1))
                if c:
                    toks.add(c)
    return toks


def verify(cfg, token) -> dict | None:
    try:
        r = httpx.get(cfg["api"].format(t=token), headers=H, timeout=12,
                      follow_redirects=True)
        if r.status_code != 200:
            return None
        jobs = r.json().get(cfg["jobs_key"], [])
        dk = [j for j in jobs if is_denmark(cfg["loc"](j))]
        if dk:
            return {"token": token, "dk": len(dk)}
    except Exception:
        return None
    return None


def load_catalog(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"_note": "", "companies": []}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in PLATFORMS:
        sys.exit(f"Использование: python tools/discover_ats.py [{'|'.join(PLATFORMS)}]")
    name = sys.argv[1]
    cfg = PLATFORMS[name]

    print(f"[{name}] перечисляю компании через архив…")
    tokens = enumerate_tokens(cfg)
    print(f"[{name}] кандидатов после чистки: {len(tokens)}")

    found = []
    checked = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(verify, cfg, t): t for t in tokens}
        for fut in as_completed(futs):
            checked += 1
            res = fut.result()
            if res:
                found.append(res)
                print(f"  + {res['token']:24} DK={res['dk']}")
            if checked % 100 == 0:
                print(f"  …проверено {checked}/{len(tokens)}, найдено DK={len(found)}")

    print(f"\n[{name}] с датскими вакансиями: {len(found)}")
    # слить в каталог (не трогая отключённые/существующие имена)
    cat = load_catalog(cfg["catalog"])
    idf = cfg["id_field"]
    existing = {c.get(idf) for c in cat.get("companies", [])}
    added = 0
    for res in sorted(found, key=lambda x: -x["dk"]):
        if res["token"] not in existing:
            cat["companies"].append({idf: res["token"], "name": res["token"], "enabled": True})
            existing.add(res["token"])
            added += 1
    cfg["catalog"].write_text(json.dumps(cat, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    print(f"[{name}] добавлено новых в каталог: {added}. Всего в каталоге: {len(cat['companies'])}")


if __name__ == "__main__":
    main()
