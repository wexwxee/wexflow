"""Read application truth from the official Salling Candidate Career Cockpit."""
from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import config
from json_store import atomic_write_json, read_json


PORTAL_URL = "https://candidatecareercockpit-a3r1eyssyw.dispatcher.hana.ondemand.com/"
STATE_PATH = config.DATA_DIR / "salling_monitor.json"
LOCK_PATH = config.DATA_DIR / "salling_monitor.lock"

_STATUS_PATTERNS = (
    ("offer", "Предложение о работе", (
        r"\boffer\b", r"job offer", r"tilbud om ansættelse", r"ansættelsestilbud",
    )),
    ("interview", "Приглашение на собеседование", (
        r"\binterview\b", r"jobsamtale", r"invited", r"inviteret", r"samtale",
    )),
    ("rejected", "Отказ", (
        r"\brejected\b", r"not selected", r"no longer under consideration",
        r"afslag", r"afvist", r"ikke udvalgt", r"ikke længere i betragtning",
    )),
    ("applied", "Заявка подана", (
        r"^applied$", r"application received", r"ansøgning modtaget",
        r"under behandling", r"in process", r"under review",
    )),
)
_ACCOUNT_MARKERS = (
    "min profil", "søgte stillinger", "jobs applied", "my profile",
    "du kan følge dine ansøgninger her", "log ud",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_state() -> dict:
    return {
        "version": 1,
        "enabled": False,
        "connected": False,
        "phase": "off",
        "last_checked_at": "",
        "last_success_at": "",
        "last_error": "",
        "reported_count": 0,
        "applications": {},
    }


def load_state() -> dict:
    state = read_json(STATE_PATH, _default_state(), dict)
    result = _default_state()
    result.update(state or {})
    if not isinstance(result.get("applications"), dict):
        result["applications"] = {}
    return result


def save_state(**changes) -> dict:
    state = load_state()
    state.update(changes)
    state["version"] = 1
    atomic_write_json(STATE_PATH, state, indent=2)
    return state


def set_enabled(enabled: bool) -> dict:
    return save_state(
        enabled=bool(enabled),
        phase="connecting" if enabled else "off",
        last_error="",
    )


def _display_time(value) -> str:
    try:
        return datetime.fromisoformat(str(value)).astimezone().strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return ""


def view() -> dict:
    state = load_state()
    phase = str(state.get("phase") or "off")
    reported_count = int(state.get("reported_count") or 0)
    if not reported_count:
        legacy_hint = re.search(r"Профиль показывает\s+(\d+)\s+заяв", str(state.get("last_error") or ""))
        reported_count = int(legacy_hint.group(1)) if legacy_hint else 0
    labels = {
        "off": "выключен",
        "connecting": "подключается",
        "connected": "подключён",
        "checking": "проверяет кабинет",
        "apply_busy": "ждёт завершения подачи",
        "needs_login": "нужно войти снова",
        "error": "ошибка проверки",
    }
    return {
        "enabled": bool(state.get("enabled")),
        "connected": bool(state.get("connected")),
        "phase": phase,
        "phase_label": labels.get(phase, phase),
        "last_checked_at": _display_time(state.get("last_checked_at")),
        "last_success_at": _display_time(state.get("last_success_at")),
        "last_error": str(state.get("last_error") or ""),
        # The profile home card still exposes the total when SAP temporarily
        # refuses to open its applications table. Preserve that fact rather
        # than showing a misleading zero during a degraded check.
        "application_count": max(
            len(state.get("applications") or {}),
            reported_count,
        ),
        "busy": is_busy(),
    }


def _lock_owner_alive() -> bool:
    try:
        pid = int(LOCK_PATH.read_text(encoding="utf-8").strip().split()[0])
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, IndexError):
        return False


def is_busy() -> bool:
    try:
        age = time.time() - LOCK_PATH.stat().st_mtime
    except OSError:
        return False
    if age >= 15 * 60 or not _lock_owner_alive():
        try:
            LOCK_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


@contextmanager
def _exclusive_run():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_busy()
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        yield False
        return
    try:
        os.write(fd, f"{os.getpid()} {_now()}".encode("utf-8"))
        os.close(fd)
        yield True
    finally:
        try:
            LOCK_PATH.unlink(missing_ok=True)
        except OSError:
            pass


def classify_status(text: str) -> dict:
    normal = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    for code, label, patterns in _STATUS_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, normal, re.I):
                return {"code": code, "label": label}
    return {"code": "unknown", "label": str(text or "Статус не распознан").strip()}


def is_logged_in(body_text: str) -> bool:
    normal = re.sub(r"\s+", " ", str(body_text or "")).strip().lower()
    return sum(marker in normal for marker in _ACCOUNT_MARKERS) >= 2


def parse_applied_jobs(body_text: str, known_jobs: list[dict]) -> list[dict]:
    """Parse the cockpit's stable five-column table: ID/status/title/date/brand."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(body_text or "").splitlines()]
    lines = [line for line in lines if line]
    try:
        start = max(i for i, line in enumerate(lines) if line.casefold() == "brand") + 1
    except ValueError:
        return []
    by_req = {str(item.get("requisition_id") or "").strip(): item for item in known_jobs}
    output = []
    index = start
    while index + 4 < len(lines):
        requisition_id = lines[index]
        if not re.fullmatch(r"\d{4,}", requisition_id):
            index += 1
            continue
        status_text, title, date_text, brand = lines[index + 1:index + 5]
        status = classify_status(status_text)
        known = by_req.get(requisition_id) or {}
        output.append({
            "job_id": str(known.get("id") or ""),
            "requisition_id": requisition_id,
            "title": str(known.get("title") or title),
            "brand": brand,
            "date": date_text,
            "status": status["code"],
            "status_label": status["label"],
            "portal_status": status_text,
        })
        index += 5
    return output


def applied_count_hint(body_text: str) -> int:
    """Count displayed on the main profile card, used to reject false empty tables."""
    normal = re.sub(r"\s+", " ", str(body_text or "")).strip()
    match = re.search(
        r"(?:Du kan følge dine ansøgninger her|You can follow your applications here)\s+(\d+)",
        normal,
        re.I,
    )
    return int(match.group(1)) if match else 0


def diff_snapshots(previous: dict, current: list[dict]) -> list[dict]:
    changes = []
    for item in current:
        key = item.get("job_id") or f"req:{item.get('requisition_id')}"
        old = previous.get(str(key)) or {}
        old_status = str(old.get("status") or "")
        new_status = str(item.get("status") or "")
        if old_status and old_status != "unknown" and new_status not in ("", "unknown"):
            if old_status != new_status:
                changes.append({
                    **item,
                    "previous_status": old_status,
                    "previous_label": str(old.get("status_label") or old_status),
                })
    return changes


def _known_jobs() -> list[dict]:
    from db import Job, get_session, select

    with get_session() as session:
        jobs = session.exec(select(Job).where(Job.source == "salling")).all()
    return [{
        "id": job.id,
        "title": job.title or "",
        "requisition_id": job.requisition_id or "",
    } for job in jobs]


def _application_map(items: list[dict]) -> dict:
    result = {}
    for item in items:
        key = item.get("job_id") or f"req:{item.get('requisition_id')}"
        result[str(key)] = {
            "title": item.get("title", ""),
            "requisition_id": item.get("requisition_id", ""),
            "status": item.get("status", "unknown"),
            "status_label": item.get("status_label", ""),
            "portal_status": item.get("portal_status", ""),
            "seen_at": _now(),
        }
    return result


def _persist_statuses(items: list[dict]) -> list[dict]:
    import applications
    import application_tracker
    from db import Job, get_session

    stage_map = {"interview": "interview", "offer": "offer", "rejected": "rejected"}
    newly_confirmed = []
    for item in items:
        job_id = str(item.get("job_id") or "")
        portal_status = str(item.get("status") or "")
        if not job_id or portal_status in ("", "unknown"):
            continue
        with get_session() as session:
            job = session.get(Job, job_id)
            if not job:
                continue
            first_confirmation = job.applied_at is None
            if first_confirmation:
                application_tracker.set_status(job, "applied", source="salling_portal")
                job.applied_confidence = "portal"
            if portal_status in stage_map and job.status not in ("offer", "closed"):
                application_tracker.set_status(
                    job, stage_map[portal_status], source="salling_portal"
                )
            session.add(job)
            session.commit()
            if first_confirmation:
                session.refresh(job)
                applications.record_submitted([job])
                newly_confirmed.append({"id": job.id, "title": job.title or item.get("title") or "Вакансия"})
    return newly_confirmed


def _notify_changes(changes: list[dict]) -> None:
    if not changes:
        return
    import cloud_auth

    for change in changes:
        cloud_auth.send_digest(
            "🔔 <b>Salling обновил статус заявки</b>\n"
            f"{change.get('title') or 'Вакансия'}\n"
            f"{change.get('previous_label') or change.get('previous_status')} "
            f"→ <b>{change.get('status_label') or change.get('status')}</b>\n\n"
            "Источник: официальный кандидатский кабинет → Søgte stillinger."
        )


def _launch_context(playwright, *, headless: bool):
    import apply as salling_apply

    if salling_apply._browser_profile_in_use():
        raise RuntimeError("Окно подачи Salling сейчас открыто")
    config.BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    last_error = None
    args = [
        "--disable-features=AutofillServerCommunication,AutofillEnableAccountWalletStorage,PasswordManagerOnboarding",
        "--disable-save-password-bubble",
    ]
    for options in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
        try:
            return playwright.chromium.launch_persistent_context(
                user_data_dir=str(config.BROWSER_PROFILE_DIR),
                headless=headless,
                locale="da-DK",
                args=args,
                **options,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise RuntimeError(f"Не удалось открыть кабинет Salling: {last_error}")


def _page_text(page) -> str:
    chunks = []
    for frame in page.frames:
        try:
            chunks.append(frame.locator("body").inner_text(timeout=5000))
        except Exception:
            continue
    return "\n".join(chunks)


def _open_portal(page) -> None:
    page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2200)


def _login(page) -> bool:
    import apply as salling_apply

    salling_apply.try_login(page, salling_apply.load_profile())
    page.wait_for_timeout(3000)
    return is_logged_in(_page_text(page))


def _open_applied(page) -> None:
    target = page.get_by_text(re.compile(r"^(Søgte stillinger|Jobs Applied)$", re.I), exact=True)
    if target.count():
        target.last.click()
        page.wait_for_timeout(5000)


def run_login(max_seconds: int = 600) -> bool:
    with _exclusive_run() as acquired:
        if not acquired:
            return False
        save_state(enabled=True, phase="connecting", last_error="")
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                context = _launch_context(playwright, headless=False)
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    _open_portal(page)
                    deadline = time.monotonic() + max(30, int(max_seconds))
                    while time.monotonic() < deadline and not page.is_closed():
                        if _login(page):
                            save_state(
                                enabled=True, connected=True, phase="connected",
                                last_success_at=_now(), last_error="",
                            )
                            return True
                        page.wait_for_timeout(1200)
                finally:
                    context.close()
        except Exception as exc:  # noqa: BLE001
            save_state(connected=False, phase="error", last_error=str(exc)[:260])
            return False
        save_state(
            connected=False, phase="needs_login",
            last_error="Вход не завершён — открой кабинет ещё раз и войди в Salling.",
        )
        return False


def run_check() -> bool:
    state = load_state()
    if not state.get("enabled"):
        return False
    with _exclusive_run() as acquired:
        if not acquired:
            return False
        save_state(phase="checking", last_checked_at=_now(), last_error="")
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                context = _launch_context(playwright, headless=True)
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    _open_portal(page)
                    if not _login(page):
                        save_state(
                            connected=False, phase="needs_login",
                            last_error="Автовход Salling не удался — проверь сохранённый вход.",
                        )
                        return False
                    expected_count = applied_count_hint(_page_text(page))
                    _open_applied(page)
                    body = _page_text(page)
                finally:
                    context.close()
            snapshots = parse_applied_jobs(body, _known_jobs())
            if expected_count and not snapshots:
                save_state(
                    enabled=True,
                    connected=True,
                    phase="error",
                    last_checked_at=_now(),
                    reported_count=expected_count,
                    last_error=(
                        f"Профиль показывает {expected_count} заявок, но таблица Søgte "
                        "stillinger временно не открылась. Ничего не изменено; попробую снова."
                    ),
                )
                return False
            previous = state.get("applications") or {}
            current = _application_map(snapshots)
            changes = diff_snapshots(previous, snapshots)
            newly_confirmed = _persist_statuses(snapshots)
            merged = dict(previous)
            merged.update(current)
            save_state(
                enabled=True, connected=True, phase="connected",
                last_checked_at=_now(), last_success_at=_now(),
                last_error="" if snapshots else (
                    "Кабинет открыт, но таблица Søgte stillinger пока пуста."
                ),
                reported_count=expected_count or len(snapshots),
                applications=merged,
            )
            _notify_changes(changes)
            if newly_confirmed:
                import cloud_auth
                shown = "\n".join(f"• {item['title']}" for item in newly_confirmed[:8])
                extra = len(newly_confirmed) - 8
                if extra > 0:
                    shown += f"\n• и ещё {extra}"
                cloud_auth.send_digest(
                    "✅ <b>Кабинет Salling подтвердил подачу</b>\n"
                    + shown
                    + "\n\nWexFlow восстановил эти заявки в разделе «Уже поданные»."
                )
            return True
        except RuntimeError as exc:
            if "сейчас открыто" in str(exc):
                save_state(phase="apply_busy", last_error="Проверю после закрытия окна подачи Salling.")
                return False
            save_state(phase="error", last_error=str(exc)[:260])
            return False
        except Exception as exc:  # noqa: BLE001
            save_state(phase="error", last_checked_at=_now(), last_error=str(exc)[:260])
            return False
