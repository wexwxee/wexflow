"""Safe live Lidl checkpoint: fill one current form, never arm or click Ansøg."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
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

import document_rules
import config
import form_questions
import profile_store
import settings_store
from connectors import lidl_apply
from db import Job


def _required_control_diagnostics(page) -> list[dict]:
    """Describe only empty required UI controls; values are intentionally omitted."""
    try:
        return page.evaluate(
            r"""() => [...document.querySelectorAll('input, textarea, select')]
                .filter(control => {
                    if (control.type === 'file' || control.type === 'hidden'
                            || control.disabled || (control.value || '').trim()) return false;
                    const labels = control.labels ? [...control.labels] : [];
                    return control.required
                        || control.getAttribute('aria-required') === 'true'
                        || !!control.closest('.sapMInputBaseRequired, .sapMTextAreaRequired')
                        || labels.some(x => x.classList.contains('sapMLabelRequired'))
                        || control.getAttribute('aria-invalid') === 'true'
                        || !!control.closest(
                            '.sapMInputBaseError, .sapMTextAreaError, .sapMInputBaseContentWrapperError'
                        );
                })
                .slice(0, 12)
                .map(control => {
                    const labels = control.labels ? [...control.labels] : [];
                    const labelled = (control.getAttribute('aria-labelledby') || '')
                        .split(/\s+/).filter(Boolean)
                        .map(id => (document.getElementById(id) || {}).textContent || '')
                        .join(' ').trim();
                    const wrapper = control.closest(
                        '.sapMSlt, .sapMComboBoxBase, .sapMInputBase, .sapUiFormElement'
                    );
                    const visibleLabel = wrapper && wrapper.querySelector(
                        '.sapMSltLabel, .sapMInputBaseInner, [role="combobox"]'
                    );
                    return {
                        id: control.id || '',
                        tag: control.tagName.toLowerCase(),
                        type: control.type || '',
                        name: control.name || '',
                        label: labelled || labels.map(x => x.textContent || '').join(' ').trim(),
                        control_class: control.className || '',
                        wrapper_class: wrapper ? wrapper.className || '' : '',
                        visible_text: visibleLabel
                            ? (visibleLabel.textContent || visibleLabel.value || '').trim()
                            : '',
                    };
                })"""
        ) or []
    except Exception:
        return []


def _job(job_id: str = "") -> Job:
    # The installed app can be one schema version behind the working tree.
    # Read only the stable public vacancy fields instead of asking the current
    # ORM model for every newer column (for example the AI-fit fields).
    columns = (
        "id", "source", "title", "brand", "city", "street", "zip",
        "application_link", "status", "published",
    )
    connection = sqlite3.connect(str(config.DB_PATH), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        if job_id:
            row = connection.execute(
                f"SELECT {', '.join(columns)} FROM job WHERE id = ?",
                (job_id,),
            ).fetchone()
        else:
            row = connection.execute(
                f"""SELECT {', '.join(columns)} FROM job
                    WHERE source = ? AND status NOT IN (?, ?, ?)
                    ORDER BY published DESC LIMIT 1""",
                ("lidl", "closed", "hidden", "applied"),
            ).fetchone()
    finally:
        connection.close()
    job = Job(**dict(row)) if row is not None else None
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
    profile = profile_store.resolve_company_answers(profile, "lidl")
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
        # Живая проверка читает сохранённые ответы, но не должна менять банк
        # вопросов реального пользователя. Обычный воркер продолжает записывать
        # новые вопросы; запрет действует только внутри этого диагностического
        # процесса.
        original_record = form_questions.record
        form_questions.record = lambda *args, **kwargs: 0
        try:
            checkpoint = lidl_apply.prepare(
                page,
                job.application_link,
                profile,
                allow_submit=arm_submit,
            )
        finally:
            form_questions.record = original_record
        page.wait_for_timeout(1500)
        documents = checkpoint.get("documents") or {}
        cv_uploaded = str(documents.get("cv") or "")
        cover_uploaded = str(documents.get("cover") or "")
        banner_text = str(page.evaluate(
            """() => {
                const host = document.getElementById('wexflow-banner');
                return host && host.shadowRoot ? host.shadowRoot.textContent : '';
            }"""
        ) or "")
        submission_blockers = lidl_apply.blockers(page, profile)
        required_controls = (
            _required_control_diagnostics(page) if submission_blockers else []
        )
        observed_clicks = page.evaluate(
            "() => window.__wexflowObservedSubmitClicks || 0"
        )
        result = {
            "job_id": job.id,
            "brand": document_rules.brand_key(job),
            "cv": cv.name,
            "cover": cover.name,
            "cv_uploaded": cv_uploaded,
            "cover_uploaded": cover_uploaded,
            "cv_visible": bool(cv_uploaded and cv_uploaded in banner_text),
            "cover_visible": bool(cover_uploaded and cover_uploaded in banner_text),
            "submission_blockers": submission_blockers,
            "required_controls": required_controls,
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
    if result["submission_blockers"]:
        raise RuntimeError(
            "Живая форма пока не готова к отправке: "
            + "; ".join(result["submission_blockers"][:6])
            + " | controls="
            + json.dumps(result["required_controls"], ensure_ascii=False)
        )
    return result


def _use_installed_primary_profile() -> None:
    appdata = Path(os.environ.get("APPDATA") or "")
    root = appdata / "WexFlow"
    data = root / "salling"
    if not (data / "settings.json").is_file() or not (data / "jobs.db").is_file():
        raise RuntimeError("Данные установленного WexFlow не найдены.")
    config.DATA_DIR = data
    config.DB_PATH = data / "jobs.db"
    config.PROFILE_PATH = data / "profile.json"
    config.SHARED_DIR = root
    config.LEGACY_SHARED_PROFILE_PATH = root / "profile.json"
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
