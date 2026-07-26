"""Safe live Lidl checkpoint: fill one current form, never arm or click Ansøg."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from playwright.sync_api import sync_playwright
from sqlmodel import select

import document_rules
import config
import profile_store
import settings_store
from connectors import lidl_apply
from db import Job, get_session


def _job(job_id: str = "") -> Job:
    with get_session() as session:
        if job_id:
            job = session.get(Job, job_id)
        else:
            job = session.exec(
                select(Job).where(
                    Job.source == "lidl",
                    Job.status.not_in(["closed", "hidden", "applied"]),
                ).limit(1)
            ).first()
    if job is None or not job.application_link:
        raise RuntimeError("Активная Lidl-вакансия для проверки не найдена.")
    return job


def run(
    job_id: str = "",
    headless: bool = True,
    arm_submit: bool = False,
) -> dict:
    job = _job(job_id)
    profile = document_rules.resolve_profile(profile_store.load_profile(), job)
    selection = profile.get("_document_selection", {})
    cv = Path(str(profile.get("cv_path") or ""))
    cover = Path(str(profile.get("cover_letter_path") or ""))
    if not cv.is_file() or not cover.is_file():
        raise RuntimeError("Для Lidl нужны существующие CV и мотивационное письмо.")
    if (selection.get("cv") or {}).get("level") == "global":
        raise RuntimeError("Lidl CV взят из общего комплекта, а не из правила Lidl.")
    if (selection.get("cover") or {}).get("level") == "global":
        raise RuntimeError("Lidl письмо взято из общего комплекта, а не из правила Lidl.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        page = browser.new_page()
        page.add_init_script(
            """() => {
                window.__wexflowObservedSubmitClicks = 0;
                document.addEventListener('click', event => {
                    const button = event.target && event.target.closest
                        ? event.target.closest('button') : null;
                    if (button && /^(Ansøg|Send ansøgning)$/i.test(
                            (button.innerText || '').trim())) {
                        window.__wexflowObservedSubmitClicks += 1;
                    }
                }, true);
            }"""
        )
        checkpoint = lidl_apply.prepare(
            page,
            job.application_link,
            profile,
            allow_submit=arm_submit,
        )
        page.wait_for_timeout(1500)
        body = page.locator("body").inner_text()
        observed_clicks = page.evaluate(
            "() => window.__wexflowObservedSubmitClicks || 0"
        )
        result = {
            "job_id": job.id,
            "brand": document_rules.brand_key(job),
            "cv": cv.name,
            "cover": cover.name,
            "cv_visible": cv.name in body,
            "cover_visible": cover.name in body,
            "reached_submit": bool(checkpoint.get("reached_submit")),
            "submit_armed": bool(checkpoint.get("submit_armed")),
            "submit_requested": bool(checkpoint.get("submit_requested")),
            "observed_submit_clicks": int(observed_clicks),
        }
        browser.close()

    if result["brand"] != "lidl":
        raise RuntimeError(f"Ожидался бренд lidl, получен {result['brand']!r}.")
    if not result["reached_submit"]:
        raise RuntimeError("Живая форма не дошла до финальной кнопки Ansøg.")
    if result["submit_armed"] != arm_submit:
        raise RuntimeError("Режим финальной отправки не совпал с запрошенным.")
    if result["submit_requested"]:
        raise RuntimeError("Проверка неожиданно запросила реальную отправку.")
    if result["observed_submit_clicks"] != 0:
        raise RuntimeError("Безопасная проверка нажала финальную кнопку.")
    if not result["cv_visible"] or not result["cover_visible"]:
        raise RuntimeError("Lidl не показал оба выбранных документа в живой форме.")
    return result


def _use_installed_primary_profile() -> None:
    appdata = Path(os.environ.get("APPDATA") or "")
    root = appdata / "WexFlow"
    data = root / "salling"
    if not (data / "settings.json").is_file():
        raise RuntimeError("Данные установленного WexFlow не найдены.")
    settings_store.PATH = data / "settings.json"
    config.SHARED_PROFILE_PATH = root / "profile.json"
    profile_store.UPLOAD_DIR = data / "uploads"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", default="")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--arm-submit", action="store_true")
    args = parser.parse_args()
    if args.installed:
        _use_installed_primary_profile()
    print(json.dumps(
        run(
            args.job_id,
            headless=not args.headed,
            arm_submit=args.arm_submit,
        ),
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
