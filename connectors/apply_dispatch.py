"""Универсальная подача: «дай ссылку — заполню».

detect(url) распознаёт платформу по адресу, prepare() направляет в нужный
заполнитель (точный для Teamtailor, универсальный для остальных). Работает для
ЛЮБОЙ компании поддерживаемой платформы, а не только из каталога — в этом вся
сила: один автозаполнитель = вся вселенная фирм платформы.

Запуск:  python -m connectors.apply_dispatch <url-вакансии> [--keep-open]
"""
from __future__ import annotations

import re
import json
import os
import sys
import time

import paths

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# (ключ платформы, человекочитаемое имя, регэксп по адресу)
_PLATFORMS = [
    ("teamtailor", "Teamtailor", re.compile(r"\.teamtailor\.com", re.I)),
    ("greenhouse", "Greenhouse", re.compile(r"greenhouse\.io|grnh\.se", re.I)),
    ("ashby", "Ashby", re.compile(r"ashbyhq\.com", re.I)),
    ("lever", "Lever", re.compile(r"jobs\.lever\.co", re.I)),
    ("recruitee", "Recruitee", re.compile(r"\.recruitee\.com", re.I)),
    ("workable", "Workable", re.compile(r"\.workable\.com", re.I)),
    (
        "lidl_easy_apply",
        "Lidl EasyApply",
        re.compile(r"ea-lidl\.cfapps\.[^/]*hana\.ondemand\.com/easyapply", re.I),
    ),
]

def status_path(job_id: str = ""):
    """Отдельный файл рукопожатия для каждого окна подачи."""
    import hashlib
    token = hashlib.sha256(str(job_id or "default").encode("utf-8")).hexdigest()[:16]
    return paths.DATA_DIR / f"connector_apply_status_{token}.json"


def _write_status(job_id: str, state: str, message: str = "") -> None:
    payload = {
        "job_id": str(job_id or ""),
        "state": str(state or ""),
        "message": str(message or "")[:500],
        "updated_at": time.time(),
    }
    target = status_path(job_id)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass


def detect(url: str) -> str | None:
    """Вернуть ключ платформы или None, если ссылка не поддерживается."""
    for key, _name, rx in _PLATFORMS:
        if rx.search(url or ""):
            return key
    return None


def platform_name(key: str) -> str:
    for k, name, _ in _PLATFORMS:
        if k == key:
            return name
    return key or "неизвестно"


def prepare(page, url: str, profile: dict, allow_submit: bool = False) -> str:
    """Заполнить форму по ссылке. Возвращает ключ платформы (или '')."""
    key = detect(url)
    if key == "teamtailor":
        from connectors import teamtailor_apply
        teamtailor_apply.prepare(page, url, profile)
    elif key == "lidl_easy_apply":
        from connectors import lidl_apply
        lidl_apply.prepare(page, url, profile, allow_submit=allow_submit)
    elif key:
        from connectors import generic_apply
        generic_apply.prepare(page, url, profile, platform=platform_name(key))
    else:
        # неизвестная платформа — всё равно пробуем «по подписям», вдруг повезёт
        from connectors import generic_apply
        generic_apply.prepare(page, url, profile, platform="форма")
    return key or ""


def load_profile_for_job(job_id: str = "") -> dict:
    """Load the canonical profile and apply store/brand document rules."""
    from connectors.fill_common import load_profile

    profile = load_profile()
    wanted = str(job_id or "").strip()
    if not wanted:
        return profile
    try:
        import document_rules
        from db import Job, get_session

        with get_session() as session:
            job = session.get(Job, wanted)
        if job is not None:
            return document_rules.resolve_profile(profile, job)
    except Exception as exc:
        print("  не удалось выбрать персональный комплект документов:", str(exc)[:120])
    return profile


def _record_confirmed_submission(job_id: str) -> bool:
    """Persist only a strong Lidl receipt; never infer success from a closed tab."""
    wanted = str(job_id or "").strip()
    if not wanted:
        return False
    try:
        import applications
        from db import Job, get_session, utcnow

        with get_session() as session:
            job = session.get(Job, wanted)
            if job is None:
                return False
            job.status = "applied"
            job.applied_at = job.applied_at or utcnow()
            job.applied_confidence = "receipt"
            session.add(job)
            session.commit()
            session.refresh(job)
        applications.record_submitted([job])
        return True
    except Exception as exc:
        print("  не удалось записать подтверждённую подачу:", str(exc)[:120])
        return False


def _send_proof_to_chat(page, job_id: str) -> None:
    """Скрин квитанции — в чат телефона, как у Salling. Никогда не роняет подачу."""
    try:
        from datetime import datetime

        import config
        out = config.DATA_DIR / "logs" / "applied"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{datetime.now():%Y%m%d_%H%M%S}_{job_id}.png"
        page.screenshot(path=str(path), full_page=True)
        print(f"  скрин-пруф: logs/applied/{path.name}")

        import apply as _apply
        import cloud_auth
        b64 = _apply._proof_photo_b64(path)
        if not b64:
            return
        title = job_id
        try:
            from db import Job, get_session
            with get_session() as s:
                job = s.get(Job, job_id)
            if job is not None:
                title = " · ".join(x for x in [job.title, job.brand, job.city] if x) or job_id
        except Exception:  # noqa: BLE001
            pass
        cloud_auth.report_apply_proof(
            job_id, b64,
            "✅ <b>Заявка отправлена</b>\n" + str(title)
            + "\nСайт показал квитанцию — скрин страницы приложен.",
        )
    except Exception as exc:  # noqa: BLE001
        print("  скрин не ушёл в чат:", str(exc)[:120])


def _wait_until_closed(ctx, page=None, platform: str = "", job_id: str = "") -> None:
    recorded = False
    while True:
        try:
            _ = ctx.pages
            if not ctx.browser or not ctx.browser.is_connected():
                return
        except Exception:
            return
        if not recorded and platform == "lidl_easy_apply" and page is not None:
            try:
                from connectors import lidl_apply
                if lidl_apply.submission_receipt_visible(page):
                    recorded = _record_confirmed_submission(job_id)
                    _write_status(
                        job_id,
                        "submitted",
                        "Lidl подтвердил получение заявки.",
                    )
                    print("  ПОДТВЕРЖДЕНО: Lidl показал квитанцию о получении.")
                    _send_proof_to_chat(page, job_id)
            except Exception:
                pass
        time.sleep(1.0)


def run(
    url: str,
    keep_open: bool = False,
    job_id: str = "",
    submit: bool = False,
) -> None:
    from connectors.browser import launch_browser
    from playwright.sync_api import sync_playwright

    _write_status(job_id, "starting")
    try:
        profile = load_profile_for_job(job_id)
        key = detect(url)
        print(f"платформа: {platform_name(key) if key else 'не распознана (пробую универсально)'}")
        _write_status(job_id, "opening_browser")
        with sync_playwright() as p:
            ctx = launch_browser(p)
            _write_status(job_id, "browser_opened")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                prepare(page, url, profile, allow_submit=submit)
                _write_status(
                    job_id,
                    "submit_ready" if submit else "ready",
                    (
                        "Заполни оставшиеся вопросы и нажми зелёную кнопку WexFlow "
                        "для реальной отправки."
                        if submit else
                        "Форма подготовлена до финальной кнопки без отправки."
                    ),
                )
            except Exception as exc:
                # Частичное заполнение лучше закрытого окна: человек сможет
                # закончить неизвестную или изменившуюся форму вручную.
                print("  warning:", exc)
                _write_status(job_id, "ready", f"Часть полей оставлена вручную: {exc}")
            if keep_open:
                _wait_until_closed(ctx, page=page, platform=key or "", job_id=job_id)
            else:
                input("\nНажми Enter здесь, когда закончишь, чтобы закрыть браузер...")
            try:
                ctx.close()
            except Exception:
                pass
    except BaseException as exc:
        _write_status(job_id, "error", str(exc) or exc.__class__.__name__)
        raise


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit("Использование: python -m connectors.apply_dispatch <url> [--keep-open]")
    run(
        args[0],
        keep_open="--keep-open" in sys.argv,
        job_id=args[1] if len(args) > 1 else "",
        submit="--submit" in sys.argv,
    )
