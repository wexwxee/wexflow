"""Safe Lidl candidate-portal monitoring.

The user's password is never requested or stored by WexFlow.  Authentication
is performed by the user in a dedicated persistent browser profile.  The
monitor later reuses only that local browser session to read ``Søgte jobs``.
"""
from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import config
from json_store import atomic_write_json, read_json


PORTAL_URL = (
    "https://career5.successfactors.eu/career?"
    "brandUrl=dk&career_ns=job_application&company=lidlstiftuP2"
    "&rcm_site_locale=da_DK"
)
STATE_PATH = config.DATA_DIR / "lidl_monitor.json"
PROFILE_DIR = config.DATA_DIR / "lidl_monitor_browser"
LOCK_PATH = config.DATA_DIR / "lidl_monitor.lock"

_NO_AUTOFILL = [
    "--disable-features=AutofillServerCommunication,AutofillEnableAccountWalletStorage,PasswordManagerOnboarding",
    "--disable-save-password-bubble",
]
_STATUS_PATTERNS = (
    ("offer", "Предложение о работе", (
        r"\bjob offer\b", r"\btilbud om ansættelse\b", r"\bansættelsestilbud\b",
        r"\btilbudt stilling\b",
    )),
    ("interview", "Приглашение на собеседование", (
        r"\binterview\b", r"\bjobsamtale\b", r"\bsamtale\b",
        r"\btelefonisk samtale\b", r"\binviteret\b",
    )),
    ("rejected", "Отказ", (
        r"\brejected\b", r"\bafslag\b", r"\bafvist\b",
        r"\bikke udvalgt\b", r"\bikke taget i betragtning\b",
    )),
    ("applied", "На рассмотрении", (
        r"\bapplication received\b", r"\bansøgning modtaget\b",
        r"\bmodtaget\b", r"\bunder behandling\b", r"\bbehandles\b",
        r"\bscreening\b", r"\bin process\b", r"\bunder review\b",
    )),
)
_LOGIN_MARKERS = (
    "glemt adgangskode", "forgot password", "ny adgangskode",
    "brugernavn", "username",
)
_ACCOUNT_MARKERS = (
    "søgte jobs", "gemte ansøgninger", "log ud", "kandidatprofil",
    "profiloplysninger", "ansøgningsdokumenter",
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
        "applications": {},
    }


def load_state() -> dict:
    state = read_json(STATE_PATH, _default_state(), dict)
    base = _default_state()
    base.update(state or {})
    if not isinstance(base.get("applications"), dict):
        base["applications"] = {}
    return base


def save_state(**changes) -> dict:
    state = load_state()
    state.update(changes)
    state["version"] = 1
    atomic_write_json(STATE_PATH, state, indent=2)
    return state


def set_enabled(enabled: bool) -> dict:
    if enabled:
        return save_state(enabled=True, phase="connecting", last_error="")
    return save_state(enabled=False, phase="off", last_error="")


def view() -> dict:
    state = load_state()
    phase = str(state.get("phase") or "off")
    labels = {
        "off": "выключен",
        "connecting": "ожидает входа",
        "connected": "подключён",
        "checking": "проверяет кабинет",
        "needs_login": "нужно войти снова",
        "error": "ошибка проверки",
    }
    def display_time(value) -> str:
        try:
            return datetime.fromisoformat(str(value)).astimezone().strftime("%d.%m.%Y %H:%M")
        except (TypeError, ValueError):
            return ""
    return {
        "enabled": bool(state.get("enabled")),
        "connected": bool(state.get("connected")),
        "phase": phase,
        "phase_label": labels.get(phase, phase),
        "last_checked_at": display_time(state.get("last_checked_at")),
        "last_success_at": display_time(state.get("last_success_at")),
        "last_error": str(state.get("last_error") or ""),
        "application_count": len(state.get("applications") or {}),
        "busy": is_busy(),
    }


def is_busy() -> bool:
    try:
        age = time.time() - LOCK_PATH.stat().st_mtime
    except OSError:
        return False
    return age < 15 * 60


@contextmanager
def _exclusive_run():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            if time.time() - LOCK_PATH.stat().st_mtime >= 15 * 60:
                LOCK_PATH.unlink(missing_ok=True)
        except OSError:
            pass
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


def _normal(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return value.replace("\u00ad", "")


def is_logged_in(body_text: str, has_password_field: bool = False) -> bool:
    text = _normal(body_text)
    if has_password_field:
        return False
    account_hits = sum(marker in text for marker in _ACCOUNT_MARKERS)
    login_hits = sum(marker in text for marker in _LOGIN_MARKERS)
    return account_hits >= 2 and login_hits == 0


def classify_status(text: str) -> dict:
    normal = _normal(text)
    for code, label, patterns in _STATUS_PATTERNS:
        for pattern in patterns:
            match = re.search(pattern, normal, re.IGNORECASE)
            if match:
                return {
                    "code": code,
                    "label": label,
                    "matched": match.group(0),
                }
    return {"code": "unknown", "label": "Статус не распознан", "matched": ""}


def _title_tokens(title: str) -> list[str]:
    words = re.findall(r"[a-zA-ZÀ-ž0-9]+", _normal(title))
    ignored = {"og", "i", "til", "med", "the", "and", "timer"}
    return [word for word in words if len(word) >= 4 and word not in ignored][:7]


def extract_applications(body_text: str, known_jobs: list[dict]) -> list[dict]:
    """Extract conservative status snapshots around known Lidl applications.

    SuccessFactors changes its markup often, so matching is based on the
    visible text and local requisition/title.  A status is accepted only when
    a known status phrase occurs close to that application.
    """
    raw = str(body_text or "")
    normal = _normal(raw)
    anchors: dict[str, int] = {}
    for job in known_jobs:
        key = str(job.get("id") or job.get("requisition_id") or job.get("title") or "")
        requisition = str(job.get("requisition_id") or "").strip().lower()
        full_title = _normal(job.get("title") or "")
        position = normal.find(requisition) if requisition else -1
        if position < 0 and full_title:
            position = normal.find(full_title)
        if position < 0:
            positions = [
                normal.find(token) for token in _title_tokens(full_title)
                if normal.find(token) >= 0
            ]
            position = min(positions) if positions else -1
        if key and position >= 0:
            anchors[key] = position
    ordered_positions = sorted(set(anchors.values()))
    result = []
    for job in known_jobs:
        title = str(job.get("title") or "").strip()
        requisition = str(job.get("requisition_id") or "").strip()
        key = str(job.get("id") or requisition or title)
        anchor = anchors.get(key, -1)
        if anchor < 0:
            continue
        index = ordered_positions.index(anchor)
        previous = ordered_positions[index - 1] if index > 0 else None
        following = ordered_positions[index + 1] if index + 1 < len(ordered_positions) else None
        start = max(0, (previous + anchor) // 2 if previous is not None else anchor - 260)
        end = min(
            len(normal),
            (anchor + following) // 2 if following is not None else anchor + 700,
        )
        excerpt = normal[start:end]
        status = classify_status(excerpt)
        result.append({
            "job_id": str(job.get("id") or ""),
            "title": title,
            "requisition_id": requisition,
            "status": status["code"],
            "status_label": status["label"],
            "matched": status["matched"],
            "excerpt": excerpt[:900],
        })
    return result


def diff_snapshots(previous: dict, current: list[dict]) -> list[dict]:
    """Return real, non-unknown status changes; first read is a baseline."""
    changes = []
    previous = previous if isinstance(previous, dict) else {}
    for item in current:
        key = item.get("job_id") or item.get("requisition_id") or item.get("title")
        old = previous.get(key, {}) if key else {}
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
        jobs = session.exec(select(Job).where(
            Job.source == "lidl",
            Job.applied_at.is_not(None),
        )).all()
    return [{
        "id": job.id,
        "title": job.title or "",
        "requisition_id": job.requisition_id or "",
    } for job in jobs]


def _launch_context(playwright, *, headless: bool):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    last_error = None
    for options in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
        try:
            return playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=headless,
                locale="da-DK",
                args=_NO_AUTOFILL,
                **options,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise RuntimeError(f"Не удалось открыть отдельный браузер Lidl: {last_error}")


def _portal_page(context):
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(1800)
    return page


def _body(page) -> tuple[str, bool]:
    try:
        text = page.locator("body").inner_text(timeout=10_000)
    except Exception:  # noqa: BLE001
        text = ""
    try:
        has_password = page.locator("input[type=password]").count() > 0
    except Exception:  # noqa: BLE001
        has_password = False
    return text, has_password


def _first_visible(page, selectors: tuple[str, ...]):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                return locator
        except Exception:  # noqa: BLE001
            continue
    return None


def _try_saved_login(page) -> bool:
    """Fill and submit Lidl login using DPAPI-protected local credentials."""
    import lidl_credentials_store

    credentials = lidl_credentials_store.get()
    email = str(credentials.get("email") or "").strip()
    password = str(credentials.get("password") or "")
    if not email or not password:
        return False
    username = _first_visible(page, (
        "input[type=email]",
        "input[name*=username i]",
        "input[id*=username i]",
        "input[name*=email i]",
        "input[id*=email i]",
        "input[type=text]",
    ))
    password_input = _first_visible(page, ("input[type=password]",))
    if username is None or password_input is None:
        return False
    try:
        username.fill(email)
        password_input.fill(password)
        submit = _first_visible(page, (
            "button[type=submit]",
            "input[type=submit]",
            "button:has-text('Log på')",
            "button:has-text('Login')",
            "button:has-text('Sign in')",
        ))
        if submit is None:
            password_input.press("Enter")
        else:
            submit.click()
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(1800)
        return True
    except Exception:  # noqa: BLE001
        return False


def _open_applied_jobs(page) -> None:
    for selector in (
        "text=/Søgte jobs/i",
        "a:has-text('Søgte jobs')",
        "button:has-text('Søgte jobs')",
    ):
        try:
            target = page.locator(selector).first
            if target.count() and target.is_visible():
                target.click(timeout=5_000)
                page.wait_for_timeout(1500)
                return
        except Exception:  # noqa: BLE001
            continue


def run_login(max_seconds: int = 600) -> bool:
    """Open the isolated visible profile and wait for the user to log in."""
    with _exclusive_run() as acquired:
        if not acquired:
            save_state(phase="error", last_error="Окно Lidl уже открыто.")
            return False
        save_state(enabled=True, connected=False, phase="connecting", last_error="")
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                context = _launch_context(playwright, headless=False)
                try:
                    page = _portal_page(context)
                    _try_saved_login(page)
                    deadline = time.monotonic() + max(30, int(max_seconds))
                    while time.monotonic() < deadline:
                        if page.is_closed():
                            break
                        text, has_password = _body(page)
                        if is_logged_in(text, has_password):
                            save_state(
                                enabled=True,
                                connected=True,
                                phase="connected",
                                last_success_at=_now(),
                                last_error="",
                            )
                            page.wait_for_timeout(1200)
                            return True
                        page.wait_for_timeout(1200)
                finally:
                    context.close()
        except Exception as exc:  # noqa: BLE001
            save_state(
                connected=False,
                phase="error",
                last_error=str(exc)[:260],
            )
            return False
        save_state(
            connected=False,
            phase="needs_login",
            last_error="Вход не завершён. Нажми «Войти снова» и авторизуйся в Lidl.",
        )
        return False


def _application_map(items: list[dict]) -> dict:
    output = {}
    for item in items:
        key = item.get("job_id") or item.get("requisition_id") or item.get("title")
        if not key:
            continue
        output[str(key)] = {
            "title": item.get("title", ""),
            "requisition_id": item.get("requisition_id", ""),
            "status": item.get("status", "unknown"),
            "status_label": item.get("status_label", ""),
            "matched": item.get("matched", ""),
            "seen_at": _now(),
        }
    return output


def _apply_changes(changes: list[dict]) -> None:
    if not changes:
        return
    from db import Job, get_session
    import cloud_auth

    status_updates = {
        "interview": "interview",
        "offer": "offer",
        "rejected": "rejected",
    }
    for change in changes:
        new_status = str(change.get("status") or "")
        job_id = str(change.get("job_id") or "")
        if job_id and new_status in status_updates:
            with get_session() as session:
                job = session.get(Job, job_id)
                if job and job.status not in ("offer", "closed"):
                    job.status = status_updates[new_status]
                    session.add(job)
                    session.commit()
        title = str(change.get("title") or "Заявка Lidl")
        cloud_auth.send_digest(
            "🔔 <b>Lidl обновил статус заявки</b>\n"
            f"{title}\n"
            f"{change.get('previous_label') or change.get('previous_status')} "
            f"→ <b>{change.get('status_label') or new_status}</b>\n\n"
            "Открой WexFlow или кандидатский кабинет → Søgte jobs."
        )


def run_check() -> bool:
    """Read Søgte jobs once and notify only about detected status changes."""
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
                    page = _portal_page(context)
                    text, has_password = _body(page)
                    if not is_logged_in(text, has_password):
                        attempted = _try_saved_login(page)
                        text, has_password = _body(page)
                        if not is_logged_in(text, has_password):
                            save_state(
                                connected=False,
                                phase="needs_login",
                                last_error=(
                                    "Автовход Lidl не удался — проверь сохранённый email и пароль."
                                    if attempted else
                                    "Сессия Lidl закончилась — сохрани логин или войди снова."
                                ),
                            )
                            return False
                    _open_applied_jobs(page)
                    text, _ = _body(page)
                finally:
                    context.close()
            known = _known_jobs()
            snapshots = extract_applications(text, known)
            previous = state.get("applications") or {}
            current = _application_map(snapshots)
            changes = diff_snapshots(previous, snapshots)
            # Preserve applications not visible on this page instead of
            # interpreting a temporary layout/load failure as deletion.
            merged = dict(previous)
            merged.update(current)
            save_state(
                enabled=True,
                connected=True,
                phase="connected",
                last_checked_at=_now(),
                last_success_at=_now(),
                last_error="" if snapshots else (
                    "Кабинет открыт, но статусы пока не распознаны. "
                    "WexFlow попробует снова автоматически."
                ),
                applications=merged,
            )
            _apply_changes(changes)
            return True
        except Exception as exc:  # noqa: BLE001
            save_state(
                phase="error",
                last_checked_at=_now(),
                last_error=str(exc)[:260],
            )
            return False
