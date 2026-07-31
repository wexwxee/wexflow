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
from urllib.parse import urlparse

import paths
from connectors import site_contract

IDLE_CLOSE_SECONDS = 5 * 60

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


def _report_phone_status(job_id: str, state: str, message: str) -> None:
    """Show the result of a Telegram decision back in the same phone flow."""
    try:
        import cloud_auth
        cloud_auth.report_apply_result(job_id, state, message)
    except Exception:
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


def company_key(url: str, platform: str, profile: dict) -> str:
    """Best available company identity for answer consent/overrides."""
    # Lidl EasyApply is one employer-specific form regardless of whether the
    # vacancy feed calls the brand “Lidl”, “Lidl Danmark” or something similar.
    # Prefer the connector identity so the saved Lidl consents are not lost.
    if platform == "lidl_easy_apply":
        return "lidl"
    contextual = str(profile.get("_job_brand") or "").strip()
    if contextual:
        return contextual
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        parts = [part for part in parsed.path.split("/") if part]
        if platform == "teamtailor" and host.endswith(".teamtailor.com"):
            return host.removesuffix(".teamtailor.com").split(".")[-1]
        if platform in {"greenhouse", "ashby", "lever"} and parts:
            return parts[0]
        if host:
            return host.removeprefix("www.").split(".")[0]
    except Exception:
        pass
    return platform


def prepare(page, url: str, profile: dict, allow_submit: bool = False) -> str:
    """Заполнить форму по ссылке. Возвращает ключ платформы (или '')."""
    key = detect(url)
    import profile_store

    profile = profile_store.resolve_company_answers(
        profile,
        company_key(url, key or "", profile),
    )
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
            resolved = document_rules.resolve_profile(profile, job)
            # Контекст вакансии для банка вопросов: у какого магазина спросили и
            # рядовая это роль или руководящая (у руководящих вопросы свои).
            try:
                import labels

                resolved["_job_title"] = job.title or ""
                resolved["_job_source"] = job.source or "salling"
                resolved["_job_brand"] = (labels.brand(job.brand) if job.brand
                                          else (job.source or "").title())
                resolved["_job_role_kind"] = ("lead" if labels.is_leadership(job.title or "")
                                              else "regular")
            except Exception:  # noqa: BLE001 — подпись магазина не критична
                pass
            return resolved
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


def _send_prepared_proof_to_chat(page, job_id: str) -> None:
    """Скрин подготовленной анкеты + явное решение «Отправить / Отмена»."""
    _proof_to_chat(page, job_id, prepared=True, ask_send=True)


def _send_proof_to_chat(page, job_id: str) -> None:
    """Скрин квитанции — в чат телефона, как у Salling. Никогда не роняет подачу."""
    _proof_to_chat(page, job_id, prepared=False)


def _proof_to_chat(
    page,
    job_id: str,
    prepared: bool,
    note: str = "",
    ask_send: bool = False,
) -> None:
    """Снять страницу и отправить её в чат. Скрины прогона лежат отдельно от
    доказательств подачи — иначе журнал прицепит их как «отправлено»."""
    try:
        from datetime import datetime

        import config
        out = config.DATA_DIR / "logs" / ("prepared" if prepared else "applied")
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{datetime.now():%Y%m%d_%H%M%S}_{job_id}.png"
        page.screenshot(path=str(path), full_page=True)
        print(f"  скрин-пруф: logs/{out.name}/{path.name}")

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
        if note:
            caption = ("⚠️ <b>Подача остановлена</b>\n" + str(title) + "\n" + str(note)[:600])
        elif prepared:
            caption = ("🧪 <b>Анкета подготовлена</b>\n" + str(title)
                       + "\nЗаполнена на компьютере, отправка НЕ нажата. "
                         "Проверь и выбери действие кнопкой ниже.")
        else:
            caption = ("✅ <b>Заявка отправлена</b>\n" + str(title)
                       + "\nСайт показал квитанцию — скрин страницы приложен.")
        cloud_auth.report_apply_proof(job_id, b64, caption, ask_send=ask_send)
    except Exception as exc:  # noqa: BLE001
        print("  скрин не ушёл в чат:", str(exc)[:120])


def site_changed_banner(page, report: dict) -> None:
    """Плашка прямо в окне: сайт изменился, подача остановлена, что делать."""
    text = site_contract.human_message(report)
    page.evaluate(
        """(text) => {
            const box = document.createElement('div');
            box.style.cssText =
                'position:fixed;z-index:2147483647;left:16px;right:16px;top:16px;margin:auto;'
                + 'max-width:520px;background:#1b1e1f;color:#f4f6f7;border:1px solid #f5a623;'
                + 'border-radius:14px;padding:14px 16px;font:14px/1.45 system-ui;'
                + 'box-shadow:0 18px 50px rgba(0,0,0,.55);white-space:pre-line;';
            box.textContent = 'WexFlow: ' + text;
            const close = document.createElement('button');
            close.textContent = 'Понятно';
            close.style.cssText =
                'margin-top:12px;padding:8px 14px;border:0;border-radius:9px;'
                + 'background:#f5a623;color:#1b1200;font-weight:800;cursor:pointer;';
            close.addEventListener('click', () => box.remove());
            box.append(close);
            document.body.append(box);
        }""",
        text,
    )


_IDLE_TRACKER_SCRIPT = """
(() => {
    if (window.__wexflowIdleTrackerInstalled) return;
    window.__wexflowIdleTrackerInstalled = true;
    window.__wexflowLastActivity = Date.now();
    const active = () => { window.__wexflowLastActivity = Date.now(); };
    ['pointerdown', 'pointermove', 'keydown', 'input', 'change',
     'scroll', 'touchstart', 'wheel'].forEach(name =>
        window.addEventListener(name, active, {capture: true, passive: true}));
})();
"""


def _install_idle_tracker(ctx, page) -> None:
    """Track real user activity even across Lidl navigation."""
    try:
        ctx.add_init_script(_IDLE_TRACKER_SCRIPT)
    except Exception:
        pass
    try:
        page.evaluate(_IDLE_TRACKER_SCRIPT)
    except Exception:
        pass
    try:
        page.evaluate(
            """(minutes) => {
                const host = document.getElementById('wexflow-banner');
                const root = host && host.shadowRoot;
                const card = root && root.querySelector('.card');
                if (!card || root.getElementById('wexflow-idle-note')) return;
                const note = document.createElement('div');
                note.id = 'wexflow-idle-note';
                note.textContent =
                    `Окно останется открытым. Оно закроется только после ${minutes} мин бездействия.`;
                note.style.cssText =
                    'margin-top:8px;color:#aeb7b2;font-size:12px;line-height:1.35;';
                card.append(note);
            }""",
            max(1, int(IDLE_CLOSE_SECONDS // 60)),
        )
    except Exception:
        pass


def _wait_until_closed(
    ctx,
    page=None,
    platform: str = "",
    job_id: str = "",
    profile: dict | None = None,
    idle_seconds: float = IDLE_CLOSE_SECONDS,
) -> None:
    """Keep the prepared form usable until closed or idle for five minutes.

    Persistent Playwright contexts deliberately have ``ctx.browser is None``.
    Treating that as a closed browser made prepared Lidl windows disappear
    immediately. Open pages are the reliable lifetime signal here.
    """
    recorded = False
    if page is not None:
        _install_idle_tracker(ctx, page)
    while True:
        try:
            pages = list(ctx.pages)
            if not pages:
                return
        except Exception:
            return
        for current in pages:
            try:
                current.evaluate(_IDLE_TRACKER_SCRIPT)
            except Exception:
                pass
        activity = []
        for current in pages:
            try:
                activity.append(float(current.evaluate(
                    "() => Number(window.__wexflowLastActivity || Date.now())"
                )))
            except Exception:
                pass
        if activity and (time.time() * 1000.0 - max(activity)) >= idle_seconds * 1000.0:
            _write_status(
                job_id,
                "idle_closed",
                f"Окно закрыто после {int(idle_seconds // 60)} мин бездействия.",
            )
            return
        if platform == "lidl_easy_apply" and page is not None:
            try:
                from connectors import lidl_apply

                browser_requested = lidl_apply.take_explicit_submit_request(page)
            except Exception:
                browser_requested = False
            if browser_requested:
                print("  подтверждение в окне — отправляю заявку Lidl доверенным кликом")
                result = lidl_apply.submit(page, profile or {})
                lidl_apply.show_explicit_submit_result(
                    page,
                    result["state"],
                    result["message"],
                )
                if result["state"] == "submitted":
                    if job_id:
                        _record_confirmed_submission(job_id)
                        _write_status(job_id, "submitted", result["message"])
                        _report_phone_status(job_id, "submitted", result["message"])
                        proof_ready = bool(result.get("proof_ready", True))
                        if not proof_ready:
                            proof_ready = lidl_apply.prepare_submission_proof(page)
                        if proof_ready:
                            _send_proof_to_chat(page, job_id)
                        else:
                            print("  пруф не отправлен: окно обработки данных Lidl не закрылось")
                    print("  ПОДТВЕРЖДЕНО: Lidl показал квитанцию о получении.")
                    return
                if result["state"] == "blocked":
                    if job_id:
                        _write_status(job_id, "needs_answers", result["message"])
                    print("  Lidl не принял отправку:", result["message"])
                else:
                    if job_id:
                        _write_status(job_id, "no_receipt", result["message"])
                        _report_phone_status(job_id, "unconfirmed", result["message"])
                    print("  кнопка нажата, но квитанция Lidl не найдена")

        if platform == "lidl_easy_apply" and page is not None and job_id:
            try:
                import apply as _apply
                action = _apply.read_phone_decision(job_id)
            except Exception:
                action = ""
            if action == "cancel":
                message = "Отменено из Telegram — заявка не отправлена."
                _write_status(
                    job_id,
                    "prepare_cancelled",
                    message,
                )
                _report_phone_status(job_id, "prepare_cancelled", message)
                print("  отмена из Telegram — заявка НЕ отправлена, закрываю анкету")
                return
            if action == "submit":
                from connectors import lidl_apply

                print("  подтверждение из Telegram — отправляю заявку Lidl")
                result = lidl_apply.submit(page, profile or {})
                if result["state"] == "submitted":
                    _record_confirmed_submission(job_id)
                    _write_status(job_id, "submitted", result["message"])
                    _report_phone_status(job_id, "submitted", result["message"])
                    proof_ready = bool(result.get("proof_ready", True))
                    if not proof_ready:
                        proof_ready = lidl_apply.prepare_submission_proof(page)
                    if proof_ready:
                        _send_proof_to_chat(page, job_id)
                    else:
                        print("  пруф не отправлен: окно обработки данных Lidl не закрылось")
                    print("  ПОДТВЕРЖДЕНО: Lidl показал квитанцию о получении.")
                    return
                if result["state"] == "blocked":
                    _write_status(job_id, "needs_answers", result["message"])
                    _report_phone_status(
                        job_id,
                        "prepared",
                        "Lidl не принял отправку: " + result["message"]
                        + ". Анкета остаётся открытой на компьютере.",
                    )
                    _proof_to_chat(
                        page,
                        job_id,
                        prepared=True,
                        note=(
                            "Не удалось отправить: " + result["message"]
                            + ". Анкета остаётся открытой — дополни ответ и нажми "
                              "«Отправить до конца» в окне."
                        ),
                    )
                else:
                    _write_status(job_id, "no_receipt", result["message"])
                    _report_phone_status(job_id, "unconfirmed", result["message"])
                    _proof_to_chat(page, job_id, prepared=True, note=result["message"])
        if not recorded and platform == "lidl_easy_apply" and page is not None:
            try:
                from connectors import lidl_apply
                if lidl_apply.submission_receipt_visible(page):
                    proof_ready = lidl_apply.prepare_submission_proof(page)
                    recorded = _record_confirmed_submission(job_id)
                    _write_status(
                        job_id,
                        "submitted",
                        "Lidl подтвердил получение заявки.",
                    )
                    print("  ПОДТВЕРЖДЕНО: Lidl показал квитанцию о получении.")
                    if proof_ready:
                        _send_proof_to_chat(page, job_id)
                    else:
                        print("  пруф отложен: окно обработки данных Lidl не закрылось")
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
        if key:
            import profile_store
            profile = profile_store.resolve_company_answers(
                profile,
                company_key(url, key, profile),
            )
        print(f"платформа: {platform_name(key) if key else 'не распознана (пробую универсально)'}")
        _write_status(job_id, "opening_browser")
        with sync_playwright() as p:
            ctx = launch_browser(p)
            _write_status(job_id, "browser_opened")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                prepare(page, url, profile, allow_submit=submit)
                # Полная подача Lidl: жмём сами, но только когда отвечать
                # больше нечего. Иначе остаётся зелёная кнопка для человека.
                done = False
                if submit and key == "lidl_easy_apply":
                    from connectors import lidl_apply

                    result = lidl_apply.submit(page, profile)
                    if result["state"] == "submitted":
                        _record_confirmed_submission(job_id)
                        _write_status(job_id, "submitted", result["message"])
                        print("  ПОДТВЕРЖДЕНО: Lidl показал квитанцию о получении.")
                        _send_proof_to_chat(page, job_id)
                        done = True
                    elif result["state"] == "no_receipt":
                        _write_status(job_id, "no_receipt", result["message"])
                        _send_prepared_proof_to_chat(page, job_id)
                        done = True
                    else:
                        note = ("Не хватает ответов для автоматической подачи: "
                                + result["message"]
                                + ". Ответь в приложении: раздел «Вопросы анкет» "
                                  "(или «Профиль → Ответы для анкет») — и нажми «Подать» ещё раз.")
                        _write_status(job_id, "needs_answers", note)
                        _proof_to_chat(page, job_id, prepared=True, note=note)
                        done = True
                if not done:
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
                    if not submit:
                        # Старое нажатие из предыдущего окна не должно отправить
                        # новую анкету. Очищаем его до показа свежих кнопок.
                        try:
                            import apply as _apply
                            _apply.read_phone_decision(job_id)
                        except Exception:
                            pass
                        _send_prepared_proof_to_chat(page, job_id)
            except site_contract.SiteChanged as changed:
                # Работодатель переделал анкету: не заполняем, не жмём, честно
                # объясняем человеку и оставляем окно открытым для ручной подачи.
                message = site_contract.human_message(changed.report)
                print("  ЗАЩИТА:", message.replace("\n", " "))
                try:
                    site_changed_banner(page, changed.report)
                except Exception:
                    pass
                # скрин с плашкой — в чат: человек сам видит, что стало с формой
                _proof_to_chat(page, job_id, prepared=True, note=message)
                _write_status(job_id, "site_changed", site_contract.short_message(changed.report))
            except Exception as exc:
                # Частичное заполнение лучше закрытого окна: человек сможет
                # закончить неизвестную или изменившуюся форму вручную.
                print("  warning:", exc)
                _write_status(job_id, "ready", f"Часть полей оставлена вручную: {exc}")
            if keep_open:
                _wait_until_closed(
                    ctx,
                    page=page,
                    platform=key or "",
                    job_id=job_id,
                    profile=profile,
                )
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
