"""Read application truth from the official Salling Candidate Career Cockpit."""
from __future__ import annotations

import hashlib
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
    ("withdrawn", "Заявка отозвана", (
        r"application withdrawn", r"^withdrawn$",
        r"ansøgning(?:en)? (?:er )?trukket tilbage", r"trukket tilbage",
    )),
    ("rejected", "Отказ", (
        r"\brejected\b", r"not selected", r"no longer under consideration",
        r"afslag", r"afvist", r"ikke udvalgt", r"ikke længere i betragtning",
    )),
    ("hired", "Принят на работу", (
        r"^hired$",
        r"(?<!ikke )\bansat\b", r"offer accepted", r"tilbud accepteret",
        r"contract signed",
        r"kontrakt(?:en)? underskrevet",
    )),
    ("offer", "Предложение о работе", (
        r"\boffer\b", r"job offer", r"tilbud om ansættelse", r"ansættelsestilbud",
    )),
    ("interview", "Приглашение на собеседование", (
        r"\binterview\b", r"jobsamtale", r"invited", r"inviteret", r"samtale",
    )),
    ("reviewing", "На рассмотрении", (
        r"screening", r"under behandling", r"in process", r"under review",
        r"under consideration", r"i proces",
    )),
    ("applied", "Заявка подана", (
        r"^applied$", r"^submitted$", r"^ansøgt$",
        r"application received", r"ansøgning modtaget",
    )),
)
_STRONG_MATCH_STRENGTHS = frozenset({"exact_requisition"})
_PIPELINE_RANK = {
    "applied": 1,
    "reviewing": 2,
    "interview": 3,
    "offer": 4,
    "hired": 5,
}
_NEGATIVE_STAGES = frozenset({"rejected", "withdrawn"})
_ACCOUNT_MARKERS = (
    "min profil", "søgte stillinger", "jobs applied", "my profile",
    "du kan følge dine ansøgninger her", "log ud",
)
_STRONG_ACCOUNT_MARKERS = ("søgte stillinger", "jobs applied", "log ud")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_state() -> dict:
    return {
        "version": 2,
        "enabled": False,
        "connected": False,
        "phase": "off",
        "phase_started_at": "",
        "last_checked_at": "",
        "last_success_at": "",
        "last_error": "",
        "reported_count": 0,
        "applications": {},
        "pending_notifications": [],
        "last_notification_at": "",
        "last_notification_error": "",
    }


def load_state() -> dict:
    state = read_json(STATE_PATH, _default_state(), dict)
    result = _default_state()
    result.update(state or {})
    if not isinstance(result.get("applications"), dict):
        result["applications"] = {}
    if not isinstance(result.get("pending_notifications"), list):
        result["pending_notifications"] = []
    return result


def save_state(**changes) -> dict:
    state = load_state()
    if "phase" in changes and changes.get("phase") != state.get("phase"):
        changes.setdefault("phase_started_at", _now())
    state.update(changes)
    state["version"] = 2
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
    busy = is_busy()
    if phase in ("connecting", "checking") and not busy:
        try:
            started = datetime.fromisoformat(str(state.get("phase_started_at") or ""))
            age = (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            age = 999
        if age < 20:
            busy = True
        else:
            was_check = phase == "checking"
            fallback = "error" if state.get("connected") else "needs_login"
            message = (
                "Предыдущая проверка Salling прервалась. Нажми «Проверить сейчас» — сохранённый вход не удалён."
                if was_check and state.get("connected") else
                "Подключение Salling прервалось до подтверждения входа. Открой кабинет и войди снова."
            )
            state = save_state(phase=fallback, last_error=message)
            phase = fallback
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
        "busy": busy,
        "pending_notification_count": len(state.get("pending_notifications") or []),
        "last_notification_error": str(state.get("last_notification_error") or ""),
    }


def _lock_owner_alive() -> bool:
    try:
        pid = int(LOCK_PATH.read_text(encoding="utf-8").strip().split()[0])
        if pid <= 0:
            return False
        if os.name == "nt":
            # Never use os.kill(pid, 0) as a Windows existence check. CPython
            # may route it through TerminateProcess, killing the very worker
            # whose lock the UI is polling. A limited query handle is safe.
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
            )
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return ctypes.get_last_error() == 5  # access denied means alive
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
    if re.search(
        r"\bnot\s+yet\s+(?:been\s+)?hired\b|\bikke\s+endnu\s+(?:blevet\s+)?ansat\b",
        normal, re.I,
    ):
        return {"code": "unknown", "label": str(text or "Статус не распознан").strip()}
    if re.search(
        r"\bnot\s+(?:been\s+)?hired\b|\bikke\s+(?:blevet\s+)?ansat\b",
        normal, re.I,
    ):
        return {"code": "rejected", "label": "Отказ"}
    for code, label, patterns in _STATUS_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, normal, re.I):
                return {"code": code, "label": label}
    return {"code": "unknown", "label": str(text or "Статус не распознан").strip()}


def is_logged_in(body_text: str) -> bool:
    normal = re.sub(r"\s+", " ", str(body_text or "")).strip().lower()
    hits = sum(marker in normal for marker in _ACCOUNT_MARKERS)
    return hits >= 2 or any(marker in normal for marker in _STRONG_ACCOUNT_MARKERS)


def parse_applied_jobs(body_text: str, known_jobs: list[dict]) -> list[dict]:
    """Parse the cockpit table in both desktop text layouts.

    The SAP UI now exposes each visual five-column row as three accessible
    lines (``ID Status``, title, ``date brand``). Older browser builds expose
    five separate lines, so keep both forms supported.
    """
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(body_text or "").splitlines()]
    lines = [line for line in lines if line]
    try:
        start = max(i for i, line in enumerate(lines) if line.casefold() == "brand") + 1
    except ValueError:
        return []
    by_req = {str(item.get("requisition_id") or "").strip(): item for item in known_jobs}
    output = []
    index = start
    while index < len(lines):
        requisition_id = ""
        status_text = ""
        title = ""
        date_text = ""
        brand = ""
        packed_start = re.fullmatch(r"(\d{4,})\s+(.+)", lines[index])
        packed_end = (
            re.fullmatch(r"(\d{2}\.\d{2}\.\d{4})\s+(.+)", lines[index + 2])
            if index + 2 < len(lines) else None
        )
        if packed_start and packed_end:
            requisition_id, status_text = packed_start.groups()
            title = lines[index + 1]
            date_text, brand = packed_end.groups()
            index += 3
        elif re.fullmatch(r"\d{4,}", lines[index]) and index + 4 < len(lines):
            requisition_id = lines[index]
            status_text, title, date_text, brand = lines[index + 1:index + 5]
            index += 5
        else:
            index += 1
            continue
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
            "match_strength": (
                "exact_requisition" if known.get("id") else "external_requisition"
            ),
        })
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
            "match_strength": item.get("match_strength", ""),
            "seen_at": _now(),
        }
    return result


def _is_strong_portal_match(item: dict) -> bool:
    return str(item.get("match_strength") or "") in _STRONG_MATCH_STRENGTHS


def _current_stage(job, application_tracker) -> str:
    helper = getattr(application_tracker, "current_stage", None)
    if callable(helper):
        return str(helper(job) or "")
    return str(getattr(job, "application_stage", None) or job.status or "")


def _supported_stage(portal_status: str, application_tracker) -> str:
    labels = getattr(application_tracker, "STATUS_LABELS", {})
    if portal_status in labels:
        return portal_status
    return "applied" if portal_status == "reviewing" else ""


def _may_set_stage(current: str, target: str) -> bool:
    if not target or current == target:
        return bool(target)
    if target == "applied":
        return current in {"", "new", "seen", "closed", "hidden", "applied"}
    if current == "hired":
        return False
    if target == "hired":
        return True
    if target in _NEGATIVE_STAGES:
        return True
    if current in _NEGATIVE_STAGES:
        return False
    current_rank = _PIPELINE_RANK.get(current, 0)
    target_rank = _PIPELINE_RANK.get(target, 0)
    return target_rank > 0 and (current_rank == 0 or target_rank >= current_rank)


def _record_portal_confirmation(session, job, applications, Application, select) -> None:
    applications.record_submitted_in_session(session, [job])
    session.flush()
    row = session.exec(select(Application).where(
        Application.source == str(job.source or "salling"),
        Application.job_id == str(job.id),
    )).first()
    confidence = "receipt" if (
        str(job.applied_confidence or "") == "receipt"
        or (row is not None and str(row.confidence or "") == "receipt")
    ) else "portal"
    job.applied_confidence = confidence
    session.add(job)
    if row is not None and str(row.confidence or "") != confidence:
        row.confidence = confidence
        session.add(row)


def _record_portal_stage(session, job, target_status: str, before_status: str,
                         item: dict, application_tracker) -> dict:
    if not target_status:
        return {"accepted": False, "changed": False, "stage": before_status}
    if (before_status == target_status
            and str(job.application_status_source or "") == "salling_portal"):
        return {"accepted": True, "changed": False, "stage": before_status}
    raw = str(item.get("portal_status") or "").strip()
    normalized = re.sub(r"\s+", " ", raw).strip().casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    event_key = f"portal:salling:{job.id}:{target_status}:{digest}"
    recorder = getattr(application_tracker, "record_status_in_session", None)
    if callable(recorder):
        return recorder(
            session,
            job,
            target_status,
            source="salling_portal",
            origin="salling_portal",
            raw_label=raw,
            event_key=event_key,
        )
    if not _may_set_stage(before_status, target_status):
        return {"accepted": False, "changed": False, "stage": before_status}
    accepted = bool(application_tracker.set_status(
        job, target_status, source="salling_portal"
    ))
    session.add(job)
    return {
        "accepted": accepted,
        "changed": accepted and before_status != _current_stage(job, application_tracker),
        "stage": _current_stage(job, application_tracker),
    }


def _persist_statuses(items: list[dict], transitions: list[dict] | None = None) -> list[dict]:
    import applications
    import application_tracker
    from db import Application, Job, get_session, select

    newly_confirmed = []
    for item in items:
        job_id = str(item.get("job_id") or "")
        portal_status = str(item.get("status") or "")
        if not job_id or not _is_strong_portal_match(item):
            continue
        with get_session() as session:
            job = session.get(Job, job_id)
            if not job:
                continue
            before_status = _current_stage(job, application_tracker)
            first_confirmation = job.applied_at is None
            target_status = _supported_stage(portal_status, application_tracker)
            if first_confirmation and not target_status:
                target_status = "applied"
            _record_portal_stage(
                session, job, target_status, before_status, item, application_tracker
            )
            _record_portal_confirmation(
                session, job, applications, Application, select
            )
            session.commit()
            if first_confirmation:
                session.refresh(job)
                newly_confirmed.append({"id": job.id, "title": job.title or item.get("title") or "Вакансия"})
            after_status = _current_stage(job, application_tracker)
            if transitions is not None and before_status != after_status:
                previous_status = "applied" if first_confirmation and after_status != "applied" else before_status
                transitions.append({
                    "source": "salling",
                    "job_id": job.id,
                    "title": job.title or item.get("title") or "Вакансия Salling",
                    "brand": job.brand or item.get("brand") or "",
                    "city": job.city or "",
                    "url": job.application_link or "",
                    "previous_status": previous_status,
                    "previous_label": application_tracker.STATUS_LABELS.get(previous_status, ""),
                    "status": after_status,
                    "status_label": item.get("status_label") or application_tracker.STATUS_LABELS.get(after_status, after_status),
                    "portal_status": item.get("portal_status") or "",
                })
    return newly_confirmed


def _notification_key(item: dict) -> str:
    return f"{item.get('job_id') or item.get('title')}:{item.get('status')}"


def _queue_notifications(changes: list[dict]) -> list[dict]:
    state = load_state()
    pending = [item for item in (state.get("pending_notifications") or []) if isinstance(item, dict)]
    known = {_notification_key(item) for item in pending}
    for change in changes:
        key = _notification_key(change)
        if key not in known:
            pending.append(change)
            known.add(key)
    save_state(pending_notifications=pending[-100:])
    return pending


def _flush_notifications() -> bool:
    import application_tracker

    state = load_state()
    pending = [item for item in (state.get("pending_notifications") or []) if isinstance(item, dict)]
    if not pending:
        return True
    if application_tracker.notify_status_changes(pending, source_name="Salling Group"):
        save_state(
            pending_notifications=[], last_notification_at=_now(),
            last_notification_error="",
        )
        return True
    save_state(last_notification_error="Telegram пока недоступен; уведомление сохранено и будет отправлено повторно.")
    return False


def _notify_changes(changes: list[dict], *, durable: bool = False) -> bool:
    import application_tracker

    if durable:
        # New portal events already live in the SQLite outbox in the same
        # transaction as Job/Application. Drain only old JSON items, then the
        # DB queue; writing ``changes`` to JSON here would send every event twice.
        legacy_ok = _flush_notifications()
        outbox_ok = application_tracker.flush_pending_notifications(
            origin="salling_portal", source_name="Salling Group"
        )
        return legacy_ok and outbox_ok
    return application_tracker.notify_status_changes(changes, source_name="Salling Group")


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


def _active_page(context, fallback=None):
    pages = [candidate for candidate in context.pages if not candidate.is_closed()]
    return pages[-1] if pages else fallback


def _open_portal(page) -> None:
    page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2200)


def _login(page) -> bool:
    import apply as salling_apply

    salling_apply.try_login(page, salling_apply.load_profile())
    page.wait_for_timeout(3000)
    return is_logged_in(_page_text(page))


def _open_applied(page) -> None:
    # Clicking the named tile button is stable; get_by_text() now returns a
    # nested heading after SAP's layout update and clicking its last match is
    # a no-op.
    target = page.get_by_role(
        "button", name=re.compile(r"Søgte stillinger|Jobs Applied", re.I)
    )
    if not target.count():
        target = page.get_by_text(
            re.compile(r"^(Søgte stillinger|Jobs Applied)$", re.I), exact=True
        )
    if not target.count():
        return
    target.first.click(timeout=10_000)
    # The route appears before its data. Give the API enough time to replace
    # the temporary "Ingen data" placeholder with at least one row.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        body = _page_text(page)
        if re.search(
            r"(?:^|\n)\s*\d{4,}\s+(?:Applied|Submitted|Interview|Rejected|Offer|"
            r"Hired|Accepted|Withdrawn|Screening|Under\b|I proces|Afslag|Ansat|Trukket)",
            body,
            re.I,
        ):
            return
        page.wait_for_timeout(750)


def run_login(max_seconds: int = 600) -> bool:
    with _exclusive_run() as acquired:
        if not acquired:
            return False
        if not load_state().get("enabled"):
            return False
        save_state(phase="connecting", last_error="")
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                context = _launch_context(playwright, headless=False)
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    _open_portal(page)
                    # Submit saved credentials at most once. Re-submitting on
                    # every poll interrupted manual login/SSO and looked like a freeze.
                    authenticated = _login(page)
                    deadline = time.monotonic() + max(30, int(max_seconds))
                    while time.monotonic() < deadline:
                        page = _active_page(context, page)
                        if page is None or page.is_closed():
                            break
                        if authenticated or is_logged_in(_page_text(page)):
                            save_state(
                                connected=True, phase="connected",
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
            transitions: list[dict] = []
            _persist_statuses(snapshots, transitions)
            merged = dict(previous)
            merged.update(current)
            save_state(
                connected=True, phase="connected",
                last_checked_at=_now(), last_success_at=_now(),
                last_error="" if snapshots else (
                    "Кабинет открыт, но таблица Søgte stillinger пока пуста."
                ),
                reported_count=expected_count or len(snapshots),
                applications=merged,
            )
            # Delivery failures stay in the monitor state and are retried on
            # the next run instead of silently losing an employer response.
            _notify_changes(transitions, durable=True)
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
