"""Safe Lidl candidate-portal monitoring.

The user's password is never requested or stored by WexFlow.  Authentication
is performed by the user in a dedicated persistent browser profile.  The
monitor later reuses only that local browser session to read ``Søgte jobs``.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import config
from json_store import atomic_write_json, read_json


LOGIN_URL = (
    "https://career5.successfactors.eu/career?"
    "brandUrl=dk&career_ns=job_application&company=lidlstiftuP2"
    "&rcm_site_locale=da_DK"
)
PROFILE_URL = (
    "https://career5.successfactors.eu/career?"
    "company=lidlstiftuP2&career_ns=job_listing_summary"
    "&navBarLevel=MY_PROFILE&rcm_site_locale=da_DK"
)
# Compatibility for callers/tests which imported the old public entry URL.
PORTAL_URL = LOGIN_URL
STATE_PATH = config.DATA_DIR / "lidl_monitor.json"
PROFILE_DIR = config.DATA_DIR / "lidl_monitor_browser"
LOCK_PATH = config.DATA_DIR / "lidl_monitor.lock"

_NO_AUTOFILL = [
    "--disable-features=AutofillServerCommunication,AutofillEnableAccountWalletStorage,PasswordManagerOnboarding",
    "--disable-save-password-bubble",
]
_STATUS_PATTERNS = (
    ("withdrawn", "Заявка отозвана", (
        r"\bapplication withdrawn\b", r"\bwithdrawn\b",
        r"\bansøgning(?:en)? (?:er )?trukket tilbage\b", r"\btrukket tilbage\b",
    )),
    ("rejected", "Отказ", (
        r"\brejected\b", r"\bafslag\b", r"\bafvist\b",
        r"\bnot selected\b", r"\bikke udvalgt\b",
        r"\bikke taget i betragtning\b",
    )),
    ("hired", "Принят на работу", (
        r"(?<!not )\bhired\b", r"\bdu er blevet ansat\b",
        r"(?<!ikke )\bansat\b", r"\boffer accepted\b", r"\btilbud accepteret\b",
        r"\bcontract signed\b",
        r"\bkontrakt(?:en)? underskrevet\b",
    )),
    ("offer", "Предложение о работе", (
        r"\bjob offer\b", r"\btilbud om ansættelse\b", r"\bansættelsestilbud\b",
        r"\btilbudt stilling\b",
    )),
    ("interview", "Приглашение на собеседование", (
        r"\binterview\b", r"\bjobsamtale\b", r"\bsamtale\b",
        r"\btelefonisk samtale\b", r"\binviteret\b",
    )),
    ("reviewing", "На рассмотрении", (
        r"\bunder behandling\b", r"\bbehandles\b",
        r"\bscreening\b", r"\bin process\b", r"\bi proces\b",
        r"\bunder review\b", r"\bunder consideration\b",
    )),
    ("applied", "Заявка получена", (
        r"\bapplication received\b", r"\bansøgning modtaget\b",
        r"\bsubmitted\b", r"\bansøgt\b",
        # Deliberately no bare ``modtaget``: the profile also uses that word
        # for documents and messages, which is not proof of an application.
    )),
)
_ROW_BREAK_MARKERS = (
    r"\brequisition\s*id\b", r"\breq\.?\s*id\b", r"\bjob\s*-?\s*id\b",
    r"\bstillings\s*-?\s*id\b", r"\bjobnr\.?\b",
    r"\bansøgningsstatus\b", r"\bapplication\s+status\b",
    r"\bsøgte jobs\b", r"\bgemte ansøgninger\b",
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
_LOGIN_MARKERS = (
    "glemt adgangskode", "forgot password", "ny adgangskode",
    "brugernavn", "username",
)
_ACCOUNT_MARKERS = (
    "søgte jobs", "gemte ansøgninger", "log ud", "kandidatprofil",
    "profiloplysninger", "ansøgningsdokumenter",
)
_STRONG_ACCOUNT_MARKERS = ("søgte jobs", "gemte ansøgninger", "log ud")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_state() -> dict:
    return {
        "version": 3,
        "enabled": False,
        "connected": False,
        "phase": "off",
        "phase_started_at": "",
        "last_checked_at": "",
        "last_success_at": "",
        "last_error": "",
        "applications": {},
        "pending_verifications": [],
        "pending_notifications": [],
        "last_notification_at": "",
        "last_notification_error": "",
    }


def load_state() -> dict:
    state = read_json(STATE_PATH, _default_state(), dict)
    base = _default_state()
    base.update(state or {})
    if not isinstance(base.get("applications"), dict):
        base["applications"] = {}
    if not isinstance(base.get("pending_verifications"), list):
        base["pending_verifications"] = []
    if not isinstance(base.get("pending_notifications"), list):
        base["pending_notifications"] = []
    return base


def save_state(**changes) -> dict:
    state = load_state()
    if "phase" in changes and changes.get("phase") != state.get("phase"):
        changes.setdefault("phase_started_at", _now())
    state.update(changes)
    state["version"] = 3
    atomic_write_json(STATE_PATH, state, indent=2)
    return state


def set_enabled(enabled: bool) -> dict:
    if enabled:
        return save_state(enabled=True, phase="connecting", last_error="")
    return save_state(enabled=False, phase="off", last_error="")


def view() -> dict:
    state = load_state()
    phase = str(state.get("phase") or "off")
    busy = is_busy()
    # A killed Playwright worker used to leave the interface saying
    # "checking" forever.  Give a freshly spawned child a short grace period,
    # then turn an orphaned transient phase into an actionable state.
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
                "Предыдущая проверка Lidl прервалась. Нажми «Проверить сейчас» — "
                "сохранённый вход не удалён."
                if was_check and state.get("connected") else
                "Подключение Lidl прервалось до подтверждения входа. Нажми «Подключить кабинет» ещё раз."
            )
            state = save_state(phase=fallback, last_error=message)
            phase = fallback
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
        "busy": busy,
        "pending_notification_count": len(state.get("pending_notifications") or []),
        "last_notification_error": str(state.get("last_notification_error") or ""),
    }


def _lock_owner_alive() -> bool:
    try:
        raw = LOCK_PATH.read_text(encoding="utf-8").strip().split()[0]
        pid = int(raw)
        if pid <= 0:
            return False
        if os.name == "nt":
            # Unlike POSIX, os.kill(pid, 0) is not a harmless existence probe
            # on Windows: it can call TerminateProcess and kill the monitor we
            # are merely trying to observe. OpenProcess is read-only here.
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
    is_busy()  # also clears an expired lock or a lock left by a dead worker
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
    strong_hit = any(marker in text for marker in _STRONG_ACCOUNT_MARKERS)
    return login_hits == 0 and (account_hits >= 2 or strong_hit)


def classify_status(text: str) -> dict:
    collapsed = re.sub(r"\s+", " ", str(text or "")).strip().replace("\u00ad", "")
    # Negation may be several words before "hired/ansat", so a one-token
    # negative look-behind is not sufficient.  "Not yet" is not a rejection;
    # a plain explicit "not hired" is.
    pending_negative = re.search(
        r"\bnot\s+yet\s+(?:been\s+)?hired\b|\bikke\s+endnu\s+(?:blevet\s+)?ansat\b",
        collapsed, re.IGNORECASE,
    )
    if pending_negative:
        return {
            "code": "unknown", "label": "Статус не распознан",
            "matched": "", "portal_status": "",
        }
    rejected_hire = re.search(
        r"\bnot\s+(?:been\s+)?hired\b|\bikke\s+(?:blevet\s+)?ansat\b",
        collapsed, re.IGNORECASE,
    )
    if rejected_hire:
        return {
            "code": "rejected", "label": "Отказ",
            "matched": rejected_hire.group(0), "portal_status": rejected_hire.group(0),
        }
    for code, label, patterns in _STATUS_PATTERNS:
        for pattern in patterns:
            match = re.search(pattern, collapsed, re.IGNORECASE)
            if match:
                return {
                    "code": code,
                    "label": label,
                    "matched": match.group(0),
                    "portal_status": match.group(0),
                }
    return {
        "code": "unknown",
        "label": "Статус не распознан",
        "matched": "",
        "portal_status": "",
    }


def _title_tokens(title: str) -> list[str]:
    words = re.findall(r"[a-zA-ZÀ-ž0-9]+", _normal(title))
    ignored = {"og", "i", "til", "med", "the", "and", "timer"}
    return [word for word in words if len(word) >= 4 and word not in ignored][:7]


def _bounded_position(text: str, value: str) -> int:
    """Find an identifier without matching it inside a longer number/word."""
    wanted = _normal(value)
    if not wanted:
        return -1
    match = re.search(rf"(?<!\w){re.escape(wanted)}(?!\w)", text, re.IGNORECASE)
    return match.start() if match else -1


def _structured_status_titles(raw: str, known_jobs: list[dict]) -> dict[str, dict]:
    """Complete title lines mapped to their adjacent explicit status field."""
    lines = [line.strip() for line in str(raw or "").splitlines() if line.strip()]
    normalized_lines = [_normal(line) for line in lines]
    result: dict[str, dict] = {}
    for job in known_jobs:
        title = _normal(job.get("title") or "")
        if not title:
            continue
        indexes = [index for index, line in enumerate(normalized_lines) if line == title]
        if len(indexes) != 1:
            continue
        index = indexes[0]
        for candidate in lines[max(0, index - 1):index + 5]:
            match = re.search(
                r"\b(?:ansøgningsstatus|application status|status)\s*:\s*(.+)$",
                candidate,
                re.IGNORECASE,
            )
            if match:
                status = classify_status(match.group(1))
                if status["code"] != "unknown":
                    status["portal_status"] = re.sub(
                        r"\s+", " ", match.group(1)
                    ).strip()[:240]
                    result[title] = status
                    break
    return result


def _trim_status_value(value: str, matched: str = "") -> str:
    """Cut a status field where the portal starts the next row or field."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    cut = len(text)
    for marker in _ROW_BREAK_MARKERS:
        found = re.search(marker, text, re.IGNORECASE)
        if found and found.start() < cut:
            cut = found.start()
    text = text[:cut].strip(" -–—:•|").strip()
    phrase = re.search(re.escape(matched), text, re.IGNORECASE) if matched else None
    if phrase:
        kept = text[:phrase.end()]
        for word in text[phrase.end():].split():
            # A portal status continues in lower case ("Inviteret til
            # jobsamtale").  A capitalised word already belongs to the next
            # row's title and must not be shown as this application's status.
            if word[:1].isupper():
                break
            kept = f"{kept} {word}"
        text = kept
    return text.strip(" -–—:•|").strip()[:240]


def _explicit_status_for_requisition(raw: str, requisition: str) -> dict | None:
    """Read only the labelled status field belonging to one requisition.

    The candidate page can contain applications unknown to the local DB, so a
    broad excerpt risks borrowing the next row's rejection or hire.  Work on the
    original line structure: stop at the next requisition id, and read a single
    labelled field which ends with its own line.  Without such a field the row
    stays unknown instead of guessing.
    """
    wanted = str(requisition or "").strip()
    if not wanted:
        return None
    anchor = re.search(rf"(?<!\w){re.escape(wanted)}(?!\w)", raw, re.IGNORECASE)
    if not anchor:
        return None
    tail = raw[anchor.end():]
    following = re.search(r"(?<!\w)\d{4,}(?!\w)", tail)
    boundary = following.start() if following else min(len(tail), 900)
    match = re.search(
        r"\b(?:ansøgningsstatus|application\s+status|status)\s*:\s*"
        r"([^\n\r|;]{1,160})",
        tail[:boundary], re.IGNORECASE,
    )
    if not match:
        return None
    value = _trim_status_value(match.group(1))
    status = classify_status(value)
    if status["code"] == "unknown":
        return None
    status["portal_status"] = _trim_status_value(value, status["matched"])
    return status


def _anchor_for_job(normal: str, job: dict, title_counts: dict[str, int],
                    structured_titles) -> tuple[int, str]:
    """Return a portal anchor and how safely it identifies one application.

    Exact requisition IDs and a unique complete title are allowed to establish
    portal proof.  A token fallback is display-only: common retail titles such
    as ``Butiksassistent`` must never prove the wrong pending application.
    """
    requisition = str(job.get("requisition_id") or "").strip()
    position = _bounded_position(normal, requisition)
    if position >= 0:
        return position, "exact_requisition"

    full_title = _normal(job.get("title") or "")
    if (len(full_title) >= 12 and title_counts.get(full_title, 0) == 1
            and normal.count(full_title) == 1):
        position = normal.find(full_title)
        if position >= 0:
            return position, (
                "structured_title" if full_title in structured_titles else "exact_title"
            )

    candidates = []
    for token in sorted(_title_tokens(full_title), key=len, reverse=True):
        token_position = _bounded_position(normal, token)
        if token_position >= 0:
            # Prefer a distinctive token over the first occurrence of a very
            # common role word.  It remains fuzzy and cannot create proof.
            candidates.append((normal.count(token), -len(token), token_position))
    if candidates:
        return min(candidates)[2], "fuzzy_title"
    return -1, "none"


def extract_applications(body_text: str, known_jobs: list[dict]) -> list[dict]:
    """Extract conservative status snapshots around known Lidl applications.

    SuccessFactors changes its markup often, so matching is based on the
    visible text and local requisition/title.  A status is accepted only when
    a known status phrase occurs close to that application.
    """
    raw = str(body_text or "")
    collapsed = re.sub(r"\s+", " ", raw).strip().replace("\u00ad", "")
    normal = collapsed.lower()
    structured_statuses = _structured_status_titles(raw, known_jobs)
    title_counts: dict[str, int] = {}
    for job in known_jobs:
        title = _normal(job.get("title") or "")
        if title:
            title_counts[title] = title_counts.get(title, 0) + 1
    anchors: dict[str, tuple[int, str]] = {}
    for job in known_jobs:
        key = str(job.get("id") or job.get("requisition_id") or job.get("title") or "")
        position, strength = _anchor_for_job(
            normal, job, title_counts, structured_statuses
        )
        if key and position >= 0:
            anchors[key] = (position, strength)
    ordered_positions = sorted({position for position, _strength in anchors.values()})
    result = []
    for job in known_jobs:
        title = str(job.get("title") or "").strip()
        requisition = str(job.get("requisition_id") or "").strip()
        key = str(job.get("id") or requisition or title)
        anchor, match_strength = anchors.get(key, (-1, "none"))
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
        excerpt = collapsed[start:end]
        if match_strength == "structured_title":
            status = structured_statuses.get(_normal(title))
        elif match_strength == "exact_requisition":
            status = _explicit_status_for_requisition(raw, requisition)
        else:
            status = classify_status(excerpt)
        status = status or {
            "code": "unknown", "label": "Статус не распознан",
            "matched": "", "portal_status": "",
        }
        result.append({
            "job_id": str(job.get("id") or ""),
            "title": title,
            "requisition_id": requisition,
            "status": status["code"],
            "status_label": status["label"],
            "matched": status["matched"],
            "portal_status": status["portal_status"],
            "match_strength": match_strength,
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

    pending = {
        str(item or "").strip()
        for item in (load_state().get("pending_verifications") or [])
        if str(item or "").strip()
    }
    with get_session() as session:
        jobs = session.exec(select(Job).where(Job.source == "lidl")).all()
        jobs = [job for job in jobs if job.applied_at is not None or job.id in pending]
    return [{
        "id": job.id,
        "title": job.title or "",
        "requisition_id": job.requisition_id or "",
    } for job in jobs]


def queue_verification(job_id: str) -> bool:
    """Remember a clicked/no-receipt application until the portal confirms it."""
    wanted = str(job_id or "").strip()
    if not wanted:
        return False
    state = load_state()
    pending = [str(item) for item in (state.get("pending_verifications") or [])]
    if wanted not in pending:
        pending.append(wanted)
    save_state(pending_verifications=pending[-100:])
    return True


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
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(1800)
    return page


def _profile_page(context, page):
    """Open Lidl's authenticated candidate profile after the login redirect.

    SuccessFactors sends a valid Lidl login back to a public/expired vacancy
    page.  That page has no account markers, so checking it made a successful
    login look like a failure.  The stable MY_PROFILE route exposes the real
    session and the ``Søgte jobs`` section.
    """
    page = _active_page(context, page)
    if page is None or page.is_closed():
        page = context.new_page()
    page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(1800)
    return page


def _body(page) -> tuple[str, bool]:
    chunks = []
    for frame in page.frames:
        try:
            chunks.append(frame.locator("body").inner_text(timeout=5_000))
        except Exception:  # noqa: BLE001
            continue
    text = "\n".join(chunks)
    try:
        has_password = page.locator("input[type=password]").count() > 0
    except Exception:  # noqa: BLE001
        has_password = False
    return text, has_password


def _active_page(context, fallback=None):
    """Return the newest live tab (SSO sometimes completes in a new tab)."""
    pages = [candidate for candidate in context.pages if not candidate.is_closed()]
    return pages[-1] if pages else fallback


def _wait_until_authenticated(context, page, timeout_seconds: float = 18) -> tuple[bool, object]:
    deadline = time.monotonic() + max(1, float(timeout_seconds))
    current = page
    while time.monotonic() < deadline:
        current = _active_page(context, current)
        if current is None or current.is_closed():
            return False, current
        text, has_password = _body(current)
        if is_logged_in(text, has_password):
            return True, current
        current.wait_for_timeout(750)
    return False, current


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
        # The parent enables monitoring before spawning this worker. If the
        # user disabled it while the child was starting, do not turn it back on.
        if not load_state().get("enabled"):
            return False
        save_state(connected=False, phase="connecting", last_error="")
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                context = _launch_context(playwright, headless=False)
                try:
                    page = _portal_page(context)
                    attempted_saved_login = _try_saved_login(page)
                    if attempted_saved_login:
                        page = _profile_page(context, page)
                    deadline = time.monotonic() + max(30, int(max_seconds))
                    next_profile_probe = 0.0
                    while time.monotonic() < deadline:
                        page = _active_page(context, page)
                        if page is None or page.is_closed():
                            break
                        text, has_password = _body(page)
                        if is_logged_in(text, has_password):
                            save_state(
                                connected=True,
                                phase="connected",
                                last_success_at=_now(),
                                last_error="",
                            )
                            page.wait_for_timeout(1200)
                            return True
                        # A successful Lidl submit lands on a public vacancy.
                        # Probe the authenticated profile only after the login
                        # form disappears so manual typing is never disrupted.
                        now = time.monotonic()
                        if not has_password and now >= next_profile_probe:
                            page = _profile_page(context, page)
                            next_profile_probe = now + 4.0
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
            "portal_status": item.get("portal_status") or item.get("matched", ""),
            "match_strength": item.get("match_strength", ""),
            "seen_at": _now(),
        }
    return output


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
    # Compatibility while an older shared tracker is still running. Seeing an
    # exact row proves submission, but must not invent an unsupported outcome.
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
    """Upgrade Job and Application proof together without weakening receipts."""
    applications.record_submitted_in_session(session, [job])
    session.flush()
    row = session.exec(select(Application).where(
        Application.source == str(job.source or "lidl"),
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
            and str(job.application_status_source or "") == "lidl_portal"):
        # The same exact row is read every 30 minutes. Its first event is
        # already durable; do not create a second ``stage -> same stage`` alert.
        return {"accepted": True, "changed": False, "stage": before_status}
    raw = str(item.get("portal_status") or item.get("matched") or "").strip()
    digest = hashlib.sha256(_normal(raw).encode("utf-8")).hexdigest()[:16]
    event_key = f"portal:lidl:{job.id}:{target_status}:{digest}"
    recorder = getattr(application_tracker, "record_status_in_session", None)
    if callable(recorder):
        return recorder(
            session,
            job,
            target_status,
            source="lidl_portal",
            origin="lidl_portal",
            raw_label=raw,
            event_key=event_key,
        )
    if not _may_set_stage(before_status, target_status):
        return {"accepted": False, "changed": False, "stage": before_status}
    accepted = bool(application_tracker.set_status(
        job, target_status, source="lidl_portal"
    ))
    session.add(job)
    return {
        "accepted": accepted,
        "changed": accepted and before_status != _current_stage(job, application_tracker),
        "stage": _current_stage(job, application_tracker),
    }


def _persist_statuses(items: list[dict], transitions: list[dict] | None = None) -> None:
    """Persist recognised portal stages, including the very first snapshot."""
    from db import Application, Job, get_session, select
    import applications
    import application_tracker

    verified: set[str] = set()
    for item in items:
        new_status = str(item.get("status") or "")
        job_id = str(item.get("job_id") or "")
        strong_match = _is_strong_portal_match(item)
        if not job_id or not strong_match:
            # Fuzzy title matches remain useful diagnostic snapshots, but they
            # can neither mutate an application nor clear pending verification.
            continue
        with get_session() as session:
            job = session.get(Job, job_id)
            if not job:
                continue
            before_status = _current_stage(job, application_tracker)
            first_confirmation = job.applied_at is None
            target_status = _supported_stage(new_status, application_tracker)
            if first_confirmation and not target_status:
                # An exact row in "Søgte jobs" proves the application even if
                # Lidl introduced a status label this build does not know yet.
                target_status = "applied"
            _record_portal_stage(
                session, job, target_status, before_status, item, application_tracker
            )
            _record_portal_confirmation(
                session, job, applications, Application, select
            )
            session.commit()
            session.refresh(job)
            after_status = _current_stage(job, application_tracker)
            if transitions is not None and before_status != after_status:
                previous_status = (
                    "applied" if first_confirmation and after_status != "applied"
                    else before_status
                )
                transitions.append({
                    "source": "lidl",
                    "job_id": job.id,
                    "title": job.title or item.get("title") or "Вакансия Lidl",
                    "brand": job.brand or "Lidl",
                    "city": job.city or "",
                    "url": job.application_link or "",
                    "previous_status": previous_status,
                    "previous_label": application_tracker.STATUS_LABELS.get(previous_status, ""),
                    "status": after_status,
                    "status_label": item.get("status_label") or application_tracker.STATUS_LABELS.get(after_status, after_status),
                    "portal_status": item.get("portal_status") or item.get("matched") or "",
                })
            verified.add(job_id)
    if verified:
        state = load_state()
        pending = [
            str(item) for item in (state.get("pending_verifications") or [])
            if str(item) not in verified
        ]
        save_state(pending_verifications=pending)


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
    if application_tracker.notify_status_changes(pending, source_name="Lidl"):
        save_state(
            pending_notifications=[], last_notification_at=_now(),
            last_notification_error="",
        )
        return True
    save_state(last_notification_error="Telegram пока недоступен; уведомление сохранено и будет отправлено повторно.")
    return False


def _apply_changes(changes: list[dict], *, durable: bool = False) -> list[dict]:
    if not changes:
        if durable:
            # Drain the pre-1.4.5 JSON queue once, then use the transactional
            # DB outbox for all newly observed portal events.
            _flush_notifications()
            import application_tracker
            application_tracker.flush_pending_notifications(
                origin="lidl_portal", source_name="Lidl"
            )
        return []
    import application_tracker

    transitions: list[dict] = []
    _persist_statuses(changes, transitions)
    if durable:
        # Do not enqueue ``transitions`` in JSON: record_status_in_session has
        # already committed the same notification atomically with the status.
        # Only drain legacy JSON items left by an older build.
        _flush_notifications()
        application_tracker.flush_pending_notifications(
            origin="lidl_portal", source_name="Lidl"
        )
    elif transitions:
        application_tracker.notify_status_changes(transitions, source_name="Lidl")
    return transitions


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
                        page = _profile_page(context, page)
                        authenticated, page = _wait_until_authenticated(context, page)
                        if not authenticated:
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
                    page = _profile_page(context, page)
                    _open_applied_jobs(page)
                    text, _ = _body(page)
                finally:
                    context.close()
            known = _known_jobs()
            snapshots = extract_applications(text, known)
            previous = state.get("applications") or {}
            current = _application_map(snapshots)
            # Preserve applications not visible on this page instead of
            # interpreting a temporary layout/load failure as deletion.
            merged = dict(previous)
            merged.update(current)
            save_state(
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
            # Database transitions, not fragile in-memory snapshots, decide
            # whether an alert is new. Failed Telegram delivery remains queued.
            _apply_changes(snapshots, durable=True)
            return True
        except Exception as exc:  # noqa: BLE001
            save_state(
                phase="error",
                last_checked_at=_now(),
                last_error=str(exc)[:260],
            )
            return False
