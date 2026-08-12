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


def _write_status(
    job_id: str,
    state: str,
    message: str = "",
    *,
    phone_reported: bool = False,
) -> None:
    payload = {
        "job_id": str(job_id or ""),
        "state": str(state or ""),
        "message": str(message or "")[:500],
        "updated_at": time.time(),
        "phone_reported": bool(phone_reported),
    }
    target = status_path(job_id)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass


def _report_phone_status(job_id: str, state: str, message: str) -> bool:
    """Show the result of a Telegram decision back in the same phone flow."""
    try:
        import cloud_auth
        return bool(cloud_auth.report_apply_result(job_id, state, message))
    except Exception:
        return False


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
    import time

    last_error = None
    for attempt in range(5):
        try:
            import applications
            import application_tracker
            from db import Job, get_session, utcnow

            with get_session() as session:
                job = session.get(Job, wanted)
                if job is None:
                    raise RuntimeError("вакансия временно недоступна в локальной базе")
                moment = utcnow()
                application_tracker.record_status_in_session(
                    session,
                    job,
                    "applied",
                    source="submission",
                    occurred_at=moment,
                    raw_label="Lidl показал подтверждение; сохраняю снимок",
                    event_key=(
                        f"submission:{job.source}:{job.id}:"
                        f"{moment.isoformat(timespec='microseconds')}"
                    ),
                )
                # A visible confirmation establishes the application, but
                # platform trust is earned only after exact screenshot bytes
                # are bound in SQLite. Never downgrade stronger old evidence.
                if str(job.applied_confidence or "") not in {"portal", "receipt"}:
                    job.applied_confidence = "indirect"
                session.add(job)
                applications.record_submitted_in_session(session, [job])
                session.commit()
            return True
        except Exception as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(0.5 * (attempt + 1))
    # The employer has already shown a receipt.  Persist a durable portal
    # verification request so the next monitor run can recover Job/Application
    # instead of allowing an accidental duplicate submission.
    try:
        import lidl_monitor
        lidl_monitor.queue_verification(wanted)
    except Exception:
        pass
    print("  не удалось записать подтверждённую подачу; поставил восстановление через кабинет:",
          str(last_error)[:120])
    return False


_RECEIPT_PERSIST_FAILURE = (
    "сайт показал квитанцию, локальная запись не сохранилась; проверим кабинет"
)


def _report_receipt_persist_failure(page, job_id: str) -> str:
    """Report a receipt as unconfirmed when its durable local write failed."""
    message = _RECEIPT_PERSIST_FAILURE
    phone_reported = _notify_terminal(
        page,
        job_id,
        prepared=True,
        note=message,
        state="unconfirmed",
        message=message,
    )
    _write_status(
        job_id,
        "no_receipt",
        message,
        phone_reported=phone_reported,
    )
    print("  КВИТАНЦИЯ ЕСТЬ, НО ЛОКАЛЬНАЯ ЗАПИСЬ НЕ СОХРАНИЛАСЬ; проверим кабинет")
    return message


def _send_prepared_proof_to_chat(page, job_id: str) -> bool:
    """Скрин подготовленной анкеты + явное решение «Отправить / Отмена»."""
    return _proof_to_chat(page, job_id, prepared=True, ask_send=True)


def _send_proof_to_chat(page, job_id: str) -> bool:
    """Скрин квитанции — в чат телефона, как у Salling. Никогда не роняет подачу."""
    return _proof_to_chat(page, job_id, prepared=False)


def _proof_to_chat(
    page,
    job_id: str,
    prepared: bool,
    note: str = "",
    ask_send: bool = False,
) -> bool:
    """Снять страницу и отправить её в чат. Скрины прогона лежат отдельно от
    доказательств подачи — иначе журнал прицепит их как «отправлено»."""
    try:
        from datetime import datetime

        import config
        out = config.DATA_DIR / "logs" / ("prepared" if prepared else "applied")
        out.mkdir(parents=True, exist_ok=True)
        safe_job_id = re.sub(r"[^0-9A-Za-zА-Яа-я._-]+", "_", str(job_id or "job"))
        path = out / f"{datetime.now():%Y%m%d_%H%M%S}_{safe_job_id}.png"
        page.screenshot(path=str(path), full_page=True)
        if not prepared:
            # Raw filenames are not proof.  Bind exact bytes to the submitted
            # job in SQLite; failure here safely leaves platform trust locked.
            import trust
            trust.attach_receipt_screen(job_id, path)
        print(f"  скрин-пруф: logs/{out.name}/{path.name}")

        import apply as _apply
        import cloud_auth
        b64 = _apply._proof_photo_b64(path)
        if not b64:
            return False
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
        return bool(cloud_auth.report_apply_proof(job_id, b64, caption, ask_send=ask_send))
    except Exception as exc:  # noqa: BLE001
        print("  скрин не ушёл в чат:", str(exc)[:120])
        return False


def _notify_terminal(
    page,
    job_id: str,
    *,
    prepared: bool,
    note: str = "",
    state: str,
    message: str,
) -> bool:
    """Send exactly one phone result: screenshot first, text only as fallback."""
    sent = _proof_to_chat(page, job_id, prepared=prepared, note=note)
    if not sent:
        sent = _report_phone_status(job_id, state, message)
    return sent


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
    terminal_recorded = False
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
            if not terminal_recorded:
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
                if result["state"] == "submitted":
                    persisted = bool(
                        job_id and _record_confirmed_submission(job_id)
                    )
                    if not persisted:
                        lidl_apply.show_explicit_submit_result(
                            page, "no_receipt", _RECEIPT_PERSIST_FAILURE,
                        )
                        if job_id:
                            _report_receipt_persist_failure(page, job_id)
                        return
                    lidl_apply.show_explicit_submit_result(
                        page, result["state"], result["message"],
                    )
                    if job_id:
                        proof_ready = bool(result.get("proof_ready", True))
                        if not proof_ready:
                            proof_ready = lidl_apply.prepare_submission_proof(page)
                        phone_reported = False
                        if proof_ready:
                            phone_reported = _send_proof_to_chat(page, job_id)
                        else:
                            print("  пруф не отправлен: окно обработки данных Lidl не закрылось")
                        if not phone_reported:
                            phone_reported = _report_phone_status(
                                job_id, "submitted", result["message"]
                            )
                        _write_status(
                            job_id, "submitted", result["message"],
                            phone_reported=phone_reported,
                        )
                    print("  ПОДТВЕРЖДЕНО: Lidl показал квитанцию о получении.")
                    return
                lidl_apply.show_explicit_submit_result(
                    page,
                    result["state"],
                    result["message"],
                )
                if result["state"] == "blocked":
                    if job_id:
                        note = (
                            "Не удалось отправить: " + result["message"]
                            + ". Анкета остаётся открытой — дополни ответ и нажми "
                              "«Отправить до конца» в окне."
                        )
                        phone_reported = _notify_terminal(
                            page, job_id, prepared=True, note=note,
                            state="prepared", message=note,
                        )
                        _write_status(
                            job_id, "needs_answers", result["message"],
                            phone_reported=phone_reported,
                        )
                        terminal_recorded = True
                    print("  Lidl не принял отправку:", result["message"])
                else:
                    if job_id:
                        try:
                            import lidl_monitor
                            lidl_monitor.queue_verification(job_id)
                        except Exception:
                            pass
                        phone_reported = _notify_terminal(
                            page, job_id, prepared=True, note=result["message"],
                            state="unconfirmed", message=result["message"],
                        )
                        _write_status(
                            job_id, "no_receipt", result["message"],
                            phone_reported=phone_reported,
                        )
                        terminal_recorded = True
                    print("  кнопка нажата, но квитанция Lidl не найдена")

        if platform == "lidl_easy_apply" and page is not None and job_id:
            try:
                import apply as _apply
                action = _apply.read_phone_decision(job_id)
            except Exception:
                action = ""
            if action == "cancel":
                message = "Отменено из Telegram — заявка не отправлена."
                phone_reported = _report_phone_status(
                    job_id, "prepare_cancelled", message
                )
                _write_status(
                    job_id,
                    "prepare_cancelled",
                    message,
                    phone_reported=phone_reported,
                )
                print("  отмена из Telegram — заявка НЕ отправлена, закрываю анкету")
                return
            if action == "submit":
                from connectors import lidl_apply

                print("  подтверждение из Telegram — отправляю заявку Lidl")
                result = lidl_apply.submit(page, profile or {})
                if result["state"] == "submitted":
                    if not _record_confirmed_submission(job_id):
                        _report_receipt_persist_failure(page, job_id)
                        return
                    proof_ready = bool(result.get("proof_ready", True))
                    if not proof_ready:
                        proof_ready = lidl_apply.prepare_submission_proof(page)
                    phone_reported = False
                    if proof_ready:
                        phone_reported = _send_proof_to_chat(page, job_id)
                    else:
                        print("  пруф не отправлен: окно обработки данных Lidl не закрылось")
                    if not phone_reported:
                        phone_reported = _report_phone_status(
                            job_id, "submitted", result["message"]
                        )
                    _write_status(
                        job_id, "submitted", result["message"],
                        phone_reported=phone_reported,
                    )
                    print("  ПОДТВЕРЖДЕНО: Lidl показал квитанцию о получении.")
                    return
                if result["state"] == "blocked":
                    note = (
                        "Не удалось отправить: " + result["message"]
                        + ". Анкета остаётся открытой — дополни ответ и нажми "
                          "«Отправить до конца» в окне."
                    )
                    phone_reported = _notify_terminal(
                        page, job_id, prepared=True, note=note,
                        state="prepared", message=note,
                    )
                    _write_status(
                        job_id, "needs_answers", result["message"],
                        phone_reported=phone_reported,
                    )
                    terminal_recorded = True
                else:
                    try:
                        import lidl_monitor
                        lidl_monitor.queue_verification(job_id)
                    except Exception:
                        pass
                    phone_reported = _notify_terminal(
                        page, job_id, prepared=True, note=result["message"],
                        state="unconfirmed", message=result["message"],
                    )
                    _write_status(
                        job_id, "no_receipt", result["message"],
                        phone_reported=phone_reported,
                    )
                    terminal_recorded = True
        if not recorded and platform == "lidl_easy_apply" and page is not None:
            try:
                from connectors import lidl_apply
                if lidl_apply.submission_receipt_visible(page):
                    proof_ready = lidl_apply.prepare_submission_proof(page)
                    recorded = _record_confirmed_submission(job_id)
                    if not recorded:
                        _report_receipt_persist_failure(page, job_id)
                        return
                    print("  ПОДТВЕРЖДЕНО: Lidl показал квитанцию о получении.")
                    phone_reported = False
                    if proof_ready:
                        phone_reported = _send_proof_to_chat(page, job_id)
                    else:
                        print("  пруф отложен: окно обработки данных Lidl не закрылось")
                    if not phone_reported:
                        phone_reported = _report_phone_status(
                            job_id, "submitted", "Lidl подтвердил получение заявки."
                        )
                    _write_status(
                        job_id, "submitted", "Lidl подтвердил получение заявки.",
                        phone_reported=phone_reported,
                    )
            except Exception:
                pass
        time.sleep(1.0)


def batch_jobs(job_ids) -> list[dict]:
    """Снимок пачки: только вакансии Lidl EasyApply с рабочей ссылкой.

    Последний барьер источника для пакетной подачи. Как и у Salling, ни один
    вызывающий (включая будущий код) не может подсунуть сюда чужую платформу:
    пакетно мы жмём кнопку сами, а это разрешено только там, где заполнитель
    знает форму до последнего поля.
    """
    from db import Job, get_session

    items: list[dict] = []
    seen: set[str] = set()
    with get_session() as session:
        for raw in job_ids or []:
            jid = str(raw or "").strip()
            if not jid or jid in seen:
                continue
            seen.add(jid)
            job = session.get(Job, jid)
            if job is None:
                continue
            url = str(job.application_link or "").strip()
            if detect(url) != "lidl_easy_apply":
                print(f"  пропуск (не анкета Lidl EasyApply): {job.title or jid}")
                continue
            items.append({
                "id": job.id,
                "title": job.title or "",
                "city": job.city or "",
                "url": url,
            })
    return items


def _prepare_one(page, item: dict, profile: dict, submit: bool) -> tuple[str, str]:
    """Заполнить одну анкету пачки. Возвращает (state, message).

    Одна вакансия никогда не роняет пачку: любая беда превращается в статус
    и понятный текст, после чего очередь идёт дальше.
    """
    job_id = item["id"]
    from connectors import lidl_apply

    try:
        lidl_apply.prepare(page, item["url"], profile, allow_submit=submit)
    except site_contract.SiteChanged as changed:
        # Работодатель переделал анкету: не заполняем, не жмём, честно
        # объясняем человеку и идём к следующей вакансии.
        message = site_contract.human_message(changed.report)
        print("  ЗАЩИТА:", message.replace("\n", " "))
        try:
            site_changed_banner(page, changed.report)
        except Exception:
            pass
        phone_reported = _proof_to_chat(page, job_id, prepared=True, note=message)
        short = site_contract.short_message(changed.report)
        _write_status(job_id, "site_changed", short, phone_reported=phone_reported)
        return "site_changed", short
    except Exception as exc:
        message = f"Анкета заполнена не полностью: {str(exc)[:160]}"
        print("  warning:", exc)
        _write_status(job_id, "error", message)
        return "error", message

    if not submit:
        message = "Анкета заполнена, отправка не нажата."
        _write_status(job_id, "ready", "Форма подготовлена до финальной кнопки без отправки.")
        return "ready", message

    result = lidl_apply.submit(page, profile)
    state = result["state"]
    message = result["message"]
    if state == "submitted":
        if not _record_confirmed_submission(job_id):
            message = _report_receipt_persist_failure(page, job_id)
            return "no_receipt", message
        proof_ready = bool(result.get("proof_ready", True))
        if not proof_ready:
            proof_ready = lidl_apply.prepare_submission_proof(page)
        phone_reported = False
        if proof_ready:
            phone_reported = _send_proof_to_chat(page, job_id)
        else:
            print("  пруф не отправлен: окно обработки данных Lidl не закрылось")
        if not phone_reported:
            phone_reported = _report_phone_status(job_id, "submitted", message)
        _write_status(job_id, "submitted", message, phone_reported=phone_reported)
        print("  ПОДТВЕРЖДЕНО: Lidl показал квитанцию о получении.")
        return "submitted", message
    if state == "no_receipt":
        try:
            import lidl_monitor
            lidl_monitor.queue_verification(job_id)
        except Exception:
            pass
        phone_reported = _notify_terminal(
            page, job_id, prepared=True, note=message,
            state="unconfirmed", message=message,
        )
        _write_status(job_id, "no_receipt", message, phone_reported=phone_reported)
        print("  кнопка нажата, но квитанция Lidl не найдена")
        return "no_receipt", message
    note = ("Не хватает ответов для автоматической подачи: " + message
            + ". Ответь в приложении: раздел «Вопросы анкет» "
              "(или «Профиль → Ответы для анкет») — и подай эту вакансию ещё раз.")
    phone_reported = _notify_terminal(
        page, job_id, prepared=True, note=note,
        state="prepared", message=note,
    )
    _write_status(job_id, "needs_answers", note, phone_reported=phone_reported)
    print("  Lidl не принял отправку:", message)
    return "needs_answers", note


def run_batch(job_ids, submit: bool = False, keep_open: bool = True) -> None:
    """Пакетная подача Lidl: одно окно браузера, вакансии строго по очереди.

    Ровно та же механика, что у пакетной подачи Salling, — общий файл
    прогресса и те же статусы для телефона. Отдельный процесс на каждую
    вакансию не годился: коннекторы делят один профиль браузера и дрались бы
    за него, как когда-то дрались пачки Salling.
    """
    from connectors.browser import launch_browser
    from playwright.sync_api import sync_playwright

    import apply as _apply

    # Рукопожатие идёт по первому ПЕРЕДАННОМУ id: приложение ждёт статус
    # именно по нему, а первая вакансия могла и не пройти отбор.
    first_id = next((str(j).strip() for j in job_ids or [] if str(j or "").strip()), "")
    items = batch_jobs(job_ids)
    if not items:
        print("нечего подавать: подходящих анкет Lidl в пачке нет")
        _write_status(first_id, "error",
                      "В пачке не оказалось анкет Lidl EasyApply — подавать нечего.")
        return
    print(f"Пакетная подача Lidl: вакансий {len(items)}, "
          f"{'С ОТПРАВКОЙ' if submit else 'прогон без отправки'}")
    # Отсеянные не должны навсегда остаться «в работе»: приложение пометило
    # так всю пачку ещё до того, как воркер увидел их ссылки.
    taken = {it["id"] for it in items}
    for raw in job_ids or []:
        jid = str(raw or "").strip()
        if jid and jid not in taken:
            _mark_batch_failed(jid)

    prog_items = [{"id": it["id"], "title": it["title"], "city": it["city"],
                   "state": "pending"} for it in items]
    prog = {
        "active": True, "mode": "submit" if submit else "dry",
        "total": len(items), "done": 0, "ok": 0, "unconfirmed": 0, "failed": 0,
        "current": None, "items": prog_items,
        "started_at": _apply._now_iso(), "updated_at": _apply._now_iso(),
        "finished_at": None,
    }
    _apply._write_progress(prog)
    _apply._cloud_progress(prog)

    def _finish() -> None:
        prog["active"] = False
        prog["current"] = None
        prog["finished_at"] = _apply._now_iso()
        prog["updated_at"] = _apply._now_iso()
        _apply._write_progress(prog)
        _apply._cloud_progress(prog)

    confirmed = 0
    _write_status(first_id, "starting")
    try:
        with sync_playwright() as p:
            ctx = launch_browser(p)
            _write_status(first_id, "browser_opened")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            last_profile: dict = {}
            for i, item in enumerate(items):
                job_id = item["id"]
                print(f"\n=== {item['title']} — {item['city']} ===")
                prog["current"] = {"idx": i + 1, "id": job_id,
                                   "title": item["title"], "city": item["city"]}
                prog_items[i]["state"] = "submitting"
                prog["updated_at"] = _apply._now_iso()
                _apply._write_progress(prog)
                _apply._cloud_progress(prog)
                _apply._cloud_report(
                    job_id,
                    "submitting" if submit else "preparing",
                    "WexFlow заполняет анкету Lidl" if submit else
                    "WexFlow заполняет анкету — отправку не нажимает",
                )
                if page.is_closed():
                    page = ctx.new_page()
                # Документы выбираются под каждую вакансию отдельно: комплект
                # магазина важнее общего, поэтому профиль грузим на каждом шаге.
                last_profile = load_profile_for_job(job_id)
                state, message = _prepare_one(page, item, last_profile, submit)

                if state == "submitted":
                    confirmed += 1
                    prog_items[i]["state"] = "ok"
                    prog["ok"] += 1
                elif state == "no_receipt":
                    prog_items[i]["state"] = "unconfirmed"
                    prog["unconfirmed"] += 1
                    _mark_batch_failed(job_id)
                elif state == "ready":
                    prog_items[i]["state"] = "ok"
                    _apply._cloud_report(job_id, *_apply._prepare_report())
                else:
                    prog_items[i]["state"] = "failed"
                    prog["failed"] += 1
                    _mark_batch_failed(job_id)
                    if state == "error":
                        _apply._cloud_report(
                            job_id,
                            "failed" if submit else "prepare_failed",
                            message,
                        )
                prog["done"] = i + 1
                prog["updated_at"] = _apply._now_iso()
                _apply._write_progress(prog)
                _apply._cloud_progress(prog)
                # Чистая вкладка между анкетами: следующая форма не должна
                # видеть остатки предыдущей.
                if i + 1 < len(items):
                    try:
                        page.goto("about:blank", wait_until="domcontentloaded", timeout=10000)
                    except Exception:
                        if page.is_closed():
                            page = ctx.new_page()
            _finish()
            if submit:
                print(f"\n========\nИТОГ: Lidl подтвердил квитанцией {confirmed} из {len(items)}. "
                      "Остальные остались неподтверждёнными — их видно в списке.")
            else:
                print(f"\n========\nПрогон завершён ({len(items)} анкет) — ничего не отправлял.")
            if keep_open:
                print("\n>>> Готово. Последняя анкета остаётся открытой.")
                _wait_until_closed(
                    ctx,
                    page=page,
                    platform="lidl_easy_apply",
                    job_id=items[-1]["id"],
                    profile=last_profile,
                )
            try:
                ctx.close()
            except Exception:
                pass
    except BaseException as exc:
        _finish()
        _write_status(first_id, "error", str(exc) or exc.__class__.__name__)
        raise


def _mark_batch_failed(job_id: str) -> None:
    """Снять «в работе» с неподтверждённой заявки, не роняя пачку."""
    try:
        import applications
        applications.mark_failed([job_id], source="lidl")
    except Exception as exc:  # noqa: BLE001 — реестр не должен ронять подачу
        print("  не удалось отметить незавершённую заявку:", str(exc)[:120])


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
                        if not _record_confirmed_submission(job_id):
                            _report_receipt_persist_failure(page, job_id)
                            try:
                                ctx.close()
                            except Exception:
                                pass
                            return
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
    if "--batch" in sys.argv:
        # Пакетный режим: аргументы — id вакансий, ссылки берём из базы.
        if not args:
            sys.exit("Использование: python -m connectors.apply_dispatch --batch <job_id> [...]")
        run_batch(
            args,
            submit="--submit" in sys.argv,
            keep_open="--auto-close" not in sys.argv,
        )
    else:
        if not args:
            sys.exit("Использование: python -m connectors.apply_dispatch <url> [--keep-open]")
        run(
            args[0],
            keep_open="--keep-open" in sys.argv,
            job_id=args[1] if len(args) > 1 else "",
            submit="--submit" in sys.argv,
        )
