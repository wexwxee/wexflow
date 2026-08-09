"""Live Lidl checkpoint; submission requires three explicit command-line gates."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
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
import paths
import profile_store
import settings_store
from connectors import lidl_apply
from db import Job


def _validate_prepared(result: dict, arm_submit: bool) -> None:
    if result["brand"] != "lidl":
        raise RuntimeError(f"Ожидался бренд lidl, получен {result['brand']!r}.")
    if not result["reached_submit"]:
        raise RuntimeError("Живая форма не дошла до финальной кнопки Ansøg.")
    if result["submit_armed"] != arm_submit:
        raise RuntimeError("Режим финальной отправки не совпал с запрошенным.")
    if result["submit_requested"]:
        raise RuntimeError("Проверка неожиданно запросила реальную отправку.")
    if result["observed_submit_clicks"] != 0:
        raise RuntimeError("Безопасная подготовка нажала финальную кнопку.")
    if not result["cv_visible"] or not result["cover_visible"]:
        raise RuntimeError("Lidl не показал оба выбранных документа в живой форме.")
    if result["submission_blockers"]:
        raise RuntimeError(
            "Живая форма пока не готова к отправке: "
            + "; ".join(result["submission_blockers"][:6])
            + " | controls="
            + json.dumps(result["required_controls"], ensure_ascii=False)
        )


def _save_proof(page, job_id: str, confirmed: bool) -> Path:
    folder = "applied" if confirmed else "unconfirmed"
    out = config.DATA_DIR / "logs" / folder
    out.mkdir(parents=True, exist_ok=True)
    safe_job_id = re.sub(r"[^0-9A-Za-zА-Яа-я._-]+", "_", job_id)
    path = out / f"{datetime.now():%Y%m%d_%H%M%S}_{safe_job_id}.png"
    page.screenshot(path=str(path), full_page=True)
    return path


def _record_receipt(job_id: str) -> bool:
    """Record a confirmed receipt against both legacy and current databases."""
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    connection = sqlite3.connect(str(config.DB_PATH), timeout=30)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """UPDATE job SET status = ?, applied_at = ?, applied_confidence = ?,
               application_status_updated_at = ?, application_status_source = ?
               WHERE id = ? AND source = ?""",
            ("applied", now, "receipt", now, "submission", job_id, "lidl"),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return False
        existing = connection.execute(
            "SELECT id FROM application WHERE source = ? AND job_id = ? ORDER BY id",
            ("lidl", job_id),
        ).fetchall()
        if existing:
            ids = [int(row[0]) for row in existing]
            connection.executemany(
                """UPDATE application SET state = ?, origin = ?, confidence = ?,
                   submitted_at = ?, updated_at = ? WHERE id = ?""",
                [("submitted", "assisted", "receipt", now, now, row_id)
                 for row_id in ids],
            )
        else:
            connection.execute(
                """INSERT INTO application
                   (source, job_id, state, origin, confidence, submitted_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("lidl", job_id, "submitted", "assisted", "receipt", now, now),
            )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _send_proof_to_phone(path: Path, job: Job) -> bool:
    try:
        import cloud_auth

        photo = base64.b64encode(path.read_bytes()).decode("ascii")
        title = " · ".join(x for x in (job.title, job.brand, job.city) if x)
        caption = (
            "✅ <b>Заявка отправлена</b>\n"
            + (title or job.id)
            + "\nСайт Lidl показал квитанцию — снимок страницы приложен."
        )
        return bool(cloud_auth.report_apply_proof(job.id, photo, caption))
    except Exception as exc:
        print("  снимок сохранён локально, но не ушёл в телефон:", str(exc)[:120])
        return False


def _write_status(job_id: str, state: str, message: str, phone_reported: bool) -> None:
    token = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]
    target = paths.DATA_DIR / f"connector_apply_status_{token}.json"
    payload = {
        "job_id": job_id,
        "state": state,
        "message": message[:500],
        "updated_at": datetime.now(timezone.utc).timestamp(),
        "phone_reported": bool(phone_reported),
    }
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)


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


def _empty_select_diagnostics(page) -> list[dict]:
    """Describe empty visible SAP selects without exposing candidate values."""
    try:
        return page.evaluate(
            r"""() => [...document.querySelectorAll('.sapMSlt')]
                .filter(wrapper => {
                    const label = wrapper.querySelector('.sapMSltLabel');
                    return wrapper.getClientRects().length
                        && !((label && label.textContent) || '').trim();
                })
                .slice(0, 12)
                .map(wrapper => {
                    const ids = [wrapper.id, wrapper.id + '-hiddenInput',
                        wrapper.id + '-hiddenSelect'].filter(Boolean);
                    let label = [...document.querySelectorAll('label')].find(node =>
                        ids.includes(node.getAttribute('for') || '')
                    );
                    if (!label) {
                        const form = wrapper.closest('.sapUiFormElement, .sapUiRespGridSpanL12');
                        label = form && form.querySelector('label, .sapMLabel');
                    }
                    const native = wrapper.querySelector('select');
                    return {
                        id: wrapper.id || '',
                        wrapper_class: wrapper.className || '',
                        label: label ? (label.textContent || '').trim() : '',
                        label_class: label ? label.className || '' : '',
                        options: native
                            ? [...native.options].map(option => (option.textContent || '').trim())
                                .filter(Boolean).slice(0, 12)
                            : [],
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
    submit: bool = False,
) -> dict:
    if submit and not arm_submit:
        raise RuntimeError("Реальная отправка требует отдельный флаг --arm-submit.")
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
        empty_selects = _empty_select_diagnostics(page)
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
            "empty_selects": empty_selects,
            "reached_submit": bool(checkpoint.get("reached_submit")),
            "submit_armed": bool(checkpoint.get("submit_armed")),
            "submit_requested": bool(checkpoint.get("submit_requested")),
            "observed_submit_clicks": int(observed_clicks),
        }
        _validate_prepared(result, arm_submit)

        if submit:
            final = lidl_apply.submit(page, profile)
            result["final_state"] = str(final.get("state") or "")
            result["final_message"] = str(final.get("message") or "")
            result["observed_submit_clicks_after"] = int(page.evaluate(
                "() => window.__wexflowObservedSubmitClicks || 0"
            ))
            confirmed = result["final_state"] == "submitted"
            proof = _save_proof(page, job.id, confirmed=confirmed)
            result["proof"] = str(proof)
            if confirmed:
                result["registry_recorded"] = _record_receipt(job.id)
                result["phone_reported"] = _send_proof_to_phone(proof, job)
                _write_status(
                    job.id,
                    "submitted",
                    result["final_message"],
                    result["phone_reported"],
                )
            else:
                result["registry_recorded"] = False
                result["phone_reported"] = False
                _write_status(
                    job.id,
                    result["final_state"] or "error",
                    result["final_message"],
                    False,
                )
        browser.close()

    if submit and result.get("final_state") != "submitted":
        raise RuntimeError(
            "Lidl не показал квитанцию; повторная отправка запрещена: "
            + str(result.get("final_message") or "результат неясен")
        )
    if submit and not result.get("registry_recorded"):
        raise RuntimeError(
            "Квитанция получена и снимок сохранён, но реестр не обновился."
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
    config.LICENSE_PATH = root / "license.json"
    config.BROWSER_PROFILE_DIR = data / "browser_profile"
    config.SECRETS_PATH = data / "secrets.json"
    config.LEGACY_SHARED_PROFILE_PATH = root / "profile.json"
    settings_store.PATH = data / "settings.json"
    config.SHARED_PROFILE_PATH = root / "profile.json"
    profile_store.UPLOAD_DIR = data / "uploads"
    paths.DATA_DIR = data
    paths.SHARED_DIR = root


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", default="")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--arm-submit", action="store_true")
    parser.add_argument("--submit", action="store_true")
    args = parser.parse_args()
    if args.submit and not args.installed:
        parser.error("--submit разрешён только вместе с --installed")
    if args.submit and not args.job_id:
        parser.error("--submit требует точный --job-id")
    if args.submit and not args.arm_submit:
        parser.error("--submit требует отдельный --arm-submit")
    if args.installed:
        _use_installed_primary_profile()
    print(json.dumps(
        run(
            args.job_id,
            headless=not args.headed,
            arm_submit=args.arm_submit,
            submit=args.submit,
        ),
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
