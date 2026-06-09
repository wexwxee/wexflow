"""
Этап 0 — Discovery.

Открывает Salling Group Candidate Career Cockpit в реальном (headless) Chromium,
перехватывает ВСЕ сетевые ответы и вычленяет тот(те), что содержат данные вакансий.
Сохраняет:
  - samples/network_log.json      — список всех XHR/fetch запросов (url, метод, статус, тип)
  - samples/job_endpoint.json     — наиболее вероятный эндпоинт вакансий + пример тела ответа
  - samples/<hash>.json           — сырые JSON-ответы кандидатов

Запуск:  python discover.py
Цель:    однозначно определить URL и схему данных вакансий именно для tenant a3r1eyssyw.
"""

import asyncio
import hashlib
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright

START_URL = "https://sallinggroup.com/job/ledige-stillinger"
COCKPIT_URL = "https://candidatecareercockpit-a3r1eyssyw.dispatcher.hana.ondemand.com/"

SAMPLES = Path(__file__).parent / "samples"
SAMPLES.mkdir(exist_ok=True)

# Ключевые слова, по которым угадываем "это ответ с вакансиями"
JOB_HINT_KEYS = [
    "jobReq", "jobRequisition", "requisition", "joblist", "joblisting",
    "postings", "vacanc", "stilling", "jobTitle", "internalStatus",
    "jobs", "results", "items",
]
JOB_HINT_URL = ["job", "requisition", "search", "career", "recruit", "posting", "vacanc"]

network_log = []
candidates = []  # (url, status, body_text)


def looks_like_jobs(url: str, body: str) -> int:
    """Грубый скоринг: насколько ответ похож на данные вакансий."""
    score = 0
    u = url.lower()
    for h in JOB_HINT_URL:
        if h in u:
            score += 1
    b = body.lower()
    for k in JOB_HINT_KEYS:
        if k.lower() in b:
            score += 2
    # бонус, если это похоже на список объектов
    if re.search(r'"(results|items|jobs|d)"\s*:\s*\[', body):
        score += 3
    return score


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="da-DK",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        async def on_response(resp):
            try:
                ct = (resp.headers or {}).get("content-type", "")
                url = resp.url
                method = resp.request.method
                status = resp.status
                rec = {"url": url, "method": method, "status": status, "content_type": ct}
                network_log.append(rec)
                if "json" in ct or "xml" in ct:
                    try:
                        body = await resp.text()
                    except Exception:
                        return
                    if not body:
                        return
                    sc = looks_like_jobs(url, body)
                    if sc >= 3:
                        candidates.append((url, method, status, sc, body))
                        h = hashlib.md5(url.encode()).hexdigest()[:10]
                        (SAMPLES / f"{h}.json").write_text(
                            body[:200000], encoding="utf-8"
                        )
                        print(f"[candidate score={sc}] {method} {status} {url}")
            except Exception as e:
                print("  (response handler error)", e)

        page.on("response", on_response)

        for target in (START_URL, COCKPIT_URL):
            print(f"\n=== Открываю {target} ===")
            try:
                await page.goto(target, wait_until="networkidle", timeout=60000)
            except Exception as e:
                print("  goto warning:", e)
            # дать SPA дозагрузить вакансии
            await page.wait_for_timeout(6000)
            # попытка проскроллить/кликнуть "показать вакансии", если есть
            try:
                await page.mouse.wheel(0, 4000)
                await page.wait_for_timeout(3000)
            except Exception:
                pass

        await browser.close()

    # Сохранить полный сетевой лог
    (SAMPLES / "network_log.json").write_text(
        json.dumps(network_log, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Выбрать лучший кандидат
    candidates.sort(key=lambda x: x[3], reverse=True)
    print("\n=================  ИТОГ  =================")
    print(f"Всего сетевых ответов: {len(network_log)}")
    print(f"Кандидатов в эндпоинт вакансий: {len(candidates)}")
    if candidates:
        url, method, status, sc, body = candidates[0]
        best = {
            "url": url,
            "method": method,
            "status": status,
            "score": sc,
            "body_preview": body[:3000],
        }
        (SAMPLES / "job_endpoint.json").write_text(
            json.dumps(best, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"ЛУЧШИЙ ЭНДПОИНТ: {method} {url}  (score={sc})")
        print("Превью ответа сохранено в samples/job_endpoint.json")
    else:
        print("Эндпоинт-кандидат не найден. Смотри samples/network_log.json вручную —")
        print("возможно данные приходят через WebSocket или нестандартный content-type.")


if __name__ == "__main__":
    asyncio.run(main())
