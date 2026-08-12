"""Письмо работодателя как локальное ручное подтверждение подачи.

WexFlow не подключается к почтовому ящику и не просит пароль от него. Человек
скачивает исходное письмо в формате ``.eml`` и прикладывает его к конкретному
отклику. Заголовки загруженного файла редактируемы, поэтому такой импорт не
доказывает площадку и не разблокирует тихую автоподачу. Он лишь помогает
человеку восстановить собственный журнал, когда одновременно есть:

* результат SPF/DKIM/DMARC ``pass`` в служебных заголовках;
* фраза о получении заявки;
* связь с выбранной вакансией (ID либо достаточно характерные слова названия);
* разумная дата относительно уже известной подачи.

Исходный файл остаётся только в ``logs/email`` на компьютере.
"""
from __future__ import annotations

import hashlib
import html
import re
import uuid
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import application_tracker
import applications
import config
from db import ApplicationEvidence, Job, get_session, select, utcnow


MAX_BYTES = 2 * 1024 * 1024
EMAIL_DIR = config.DATA_DIR / "logs" / "email"


class EvidenceError(ValueError):
    """Письмо нельзя честно засчитать доказательством."""


_CONFIRMATION = re.compile(
    r"(?:"
    r"tak\s+for\s+din\s+ans(?:ø|o)gning|"
    r"vi\s+har\s+modtaget\s+(?:din\s+)?ans(?:ø|o)gning|"
    r"ans(?:ø|o)gning(?:en)?\s+(?:er|blev)\s+modtaget|"
    r"bekr(?:æ|a)ftelse\s+(?:p(?:å|a)\s+)?(?:din\s+)?ans(?:ø|o)gning|"
    r"thank\s+you\s+for\s+apply(?:ing|ing\s+for)|"
    r"we(?:\s+have|'ve)\s+received\s+your\s+application|"
    r"your\s+application\s+(?:has\s+been\s+)?received|"
    r"application\s+(?:receipt|confirmation)"
    r")",
    re.IGNORECASE,
)
_DMARC = re.compile(r"\bdmarc\s*=\s*([a-z]+)\b", re.IGNORECASE)
_HEADER_FROM = re.compile(r"\bheader\.from\s*=\s*([^\s;]+)", re.IGNORECASE)
_SAFE = re.compile(r"[^0-9A-Za-zА-Яа-я._-]+")
_WORD = re.compile(r"[0-9A-Za-zА-Яа-яÆØÅæøå]{3,}")
_STOP = {
    "job", "jobs", "stilling", "stillingen", "assistent", "medarbejder",
    "til", "hos", "med", "for", "the", "and", "with", "danmark", "denmark",
    "lidl", "salling", "group", "netto", "foetex", "føtex", "bilka", "br",
}
_SOURCE_DOMAINS = {
    "salling": {
        "sallinggroup.com", "salling.dk", "netto.dk", "foetex.dk", "føtex.dk",
        "bilka.dk", "br.dk", "hana.ondemand.com", "sap.com",
        "successfactors.com", "successfactors.eu",
    },
    "lidl": {
        "lidl.dk", "lidl.com", "successfactors.com", "successfactors.eu", "sap.com",
    },
    "teamtailor": {"teamtailor.com"},
    "greenhouse": {"greenhouse.io", "greenhouse-mail.io"},
    "ashby": {"ashbyhq.com"},
}


def _plain_part(part) -> str:
    try:
        value = part.get_content()
    except Exception:  # noqa: BLE001 — битый MIME не должен ронять журнал
        try:
            raw = part.get_payload(decode=True) or b""
            value = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""
    text = str(value or "")
    if part.get_content_type() == "text/html":
        text = re.sub(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", text,
                      flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _body(message) -> str:
    chunks: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart() or part.get_content_disposition() == "attachment":
                continue
            if part.get_content_type() in {"text/plain", "text/html"}:
                chunks.append(_plain_part(part))
    elif message.get_content_type() in {"text/plain", "text/html"}:
        chunks.append(_plain_part(message))
    return " ".join(chunks)[:250_000]


def _aligned_domain(authenticated: str, sender: str) -> bool:
    authenticated = str(authenticated or "").casefold().strip(" .")
    sender = str(sender or "").casefold().strip(" .")
    return bool(
        authenticated and sender and (
            authenticated == sender
            or authenticated.endswith("." + sender)
            or sender.endswith("." + authenticated)
        )
    )


def _authentication(message, sender_domain: str, expected: set[str]) -> str:
    """Accept only an aligned DMARC result as a useful *header signal*.

    Authentication-Results is editable text in an uploaded .eml; without a
    DNS-backed DKIM verifier or mailbox-provider metadata it is not
    cryptographic proof.  We still reject obvious spoofing (bare SPF, foreign
    alignment, conflicting DMARC failure), but persist a successful match as
    ``unverified_header`` so it can never unlock silent auto-submit.
    """
    headers = [
        str(value) for value in (message.get_all("Authentication-Results", []) or [])
    ]
    if any(match.group(1).casefold() == "fail"
           for header in headers for match in _DMARC.finditer(header)):
        return ""
    for header in headers:
        if not any(match.group(1).casefold() == "pass" for match in _DMARC.finditer(header)):
            continue
        domains = [match.group(1).casefold().strip(".")
                   for match in _HEADER_FROM.finditer(header)]
        if any(_aligned_domain(domain, sender_domain) and _domain_matches(domain, expected)
               for domain in domains):
            return "unverified_header"
    return ""


def _message_date(message) -> datetime:
    raw = str(message.get("Date") or "").strip()
    if not raw:
        raise EvidenceError("В исходном письме нет даты — его нельзя связать с подачей.")
    try:
        value = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvidenceError("Дата письма не читается.") from exc
    if value is None:
        raise EvidenceError("Дата письма не читается.")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _domain_matches(domain: str, expected: set[str]) -> bool:
    value = str(domain or "").lower().strip(".")
    return any(value == item or value.endswith("." + item) for item in expected)


def _expected_domains(job: Job) -> set[str]:
    expected = set(_SOURCE_DOMAINS.get(str(job.source or "").lower(), set()))
    try:
        host = (urlparse(str(job.application_link or "")).hostname or "").lower()
    except ValueError:
        host = ""
    if host:
        expected.add(host.removeprefix("www."))
    return expected


def _job_tokens(job: Job) -> set[str]:
    return {
        token.casefold() for token in _WORD.findall(str(job.title or ""))
        if token.casefold() not in _STOP and len(token) >= 4
    }


def _matches_job(job: Job, text: str) -> bool:
    folded = text.casefold()
    identifiers = {
        str(job.requisition_id or "").strip(),
        str(job.id or "").strip(),
        str(job.id or "").rsplit(":", 1)[-1].strip(),
    }
    if any(value and len(value) >= 4 and value.casefold() in folded for value in identifiers):
        return True
    tokens = _job_tokens(job)
    if not tokens:
        return False
    hits = sum(token in folded for token in tokens)
    return hits >= min(2, len(tokens))


def analyse(raw: bytes, job: Job) -> dict:
    """Проверить исходное письмо, ничего не сохраняя."""
    if not raw:
        raise EvidenceError("Файл письма пустой.")
    if len(raw) > MAX_BYTES:
        raise EvidenceError("Письмо больше 2 МБ. Скачай исходное письмо без вложений.")
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:  # noqa: BLE001
        raise EvidenceError("Это не читаемый файл .eml.") from exc

    subject = re.sub(r"\s+", " ", str(message.get("Subject") or "")).strip()[:240]
    sender_name, sender_address = parseaddr(str(message.get("From") or ""))
    sender_address = sender_address.strip().lower()
    sender_domain = sender_address.rsplit("@", 1)[-1] if "@" in sender_address else ""
    if not sender_domain:
        raise EvidenceError("В исходном письме нет адреса отправителя.")

    expected = _expected_domains(job)
    source = str(job.source or "").lower()
    if source in _SOURCE_DOMAINS and not _domain_matches(sender_domain, expected):
        raise EvidenceError(
            f"Письмо пришло с домена {sender_domain}, а выбранная площадка — "
            f"{source}. Такое письмо нельзя автоматически связать с этой подачей."
        )
    authentication = _authentication(message, sender_domain, expected)
    if not authentication:
        raise EvidenceError(
            "В оригинале письма нет согласованного DMARC=pass для домена отправителя. "
            "Один SPF или заголовок чужого домена не подтверждает письмо."
        )

    body = _body(message)
    searchable = f"{subject}\n{sender_name}\n{sender_address}\n{body}"
    if not _CONFIRMATION.search(searchable):
        raise EvidenceError("В письме не найдено подтверждение, что работодатель получил заявку.")
    if not _matches_job(job, searchable):
        raise EvidenceError(
            "В письме не найден ID или название выбранной вакансии. Прикрепи его к "
            "нужному отклику либо используй письмо, где вакансия указана явно."
        )

    occurred_at = _message_date(message)
    now = utcnow()
    if occurred_at > now + timedelta(days=2):
        raise EvidenceError("Дата письма находится в будущем.")
    if job.applied_at and abs(occurred_at - job.applied_at) > timedelta(days=14):
        raise EvidenceError("Дата письма слишком далеко от даты этой подачи.")

    return {
        "sender": sender_address[:240],
        "subject": subject,
        "authentication": authentication,
        "occurred_at": occurred_at,
        "fingerprint": hashlib.sha256(raw).hexdigest(),
    }


def _read_upload(upload) -> bytes:
    filename = Path(str(getattr(upload, "filename", "") or "")).name
    if Path(filename).suffix.lower() != ".eml":
        raise EvidenceError("Нужен исходный файл письма с расширением .eml.")
    stream = getattr(upload, "file", None)
    if stream is None:
        raise EvidenceError("Файл письма не прочитался.")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(256 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_BYTES:
            raise EvidenceError("Письмо больше 2 МБ. Скачай исходное письмо без вложений.")
        chunks.append(chunk)
    return b"".join(chunks)


def import_upload(job_id: str, upload) -> ApplicationEvidence:
    """Проверить и сохранить .eml, затем достроить факт подачи в реестре."""
    raw = _read_upload(upload)
    target: Path | None = None
    with get_session() as session:
        job = session.get(Job, str(job_id or ""))
        if job is None:
            raise EvidenceError("Отклик не найден.")
        checked = analyse(raw, job)
        existing = session.exec(select(ApplicationEvidence).where(
            ApplicationEvidence.fingerprint == checked["fingerprint"]
        )).first()
        if existing is not None:
            if existing.source == str(job.source or "salling") and existing.job_id == str(job.id):
                # Repair legacy/partially committed state on a retry.
                applications.record_submitted_in_session(session, [job])
                session.commit()
                return existing
            raise EvidenceError("Это письмо уже прикреплено к другому отклику.")

        EMAIL_DIR.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%d_%H%M%S")
        safe_job = _SAFE.sub("_", str(job.id or "job"))[:80].strip("._") or "job"
        # A per-attempt suffix means a losing concurrent transaction can only
        # delete its own file, never the winner's evidence artifact.
        name = (f"{stamp}_{safe_job}_{checked['fingerprint'][:10]}_"
                f"{uuid.uuid4().hex[:8]}.eml")
        target = EMAIL_DIR / name
        target.write_bytes(raw)

        evidence = ApplicationEvidence(
            source=str(job.source or "salling"),
            job_id=str(job.id),
            kind="email",
            path=name,
            fingerprint=checked["fingerprint"],
            sender=checked["sender"],
            subject=checked["subject"],
            authentication=checked["authentication"],
            occurred_at=checked["occurred_at"],
        )
        session.add(evidence)

        # Письмо может быть первым независимым фактом, что заявка вообще ушла.
        # Уже более поздний этап (интервью/оффер/отказ) назад к «Подано» не двигаем.
        if job.applied_at is None:
            job.applied_at = checked["occurred_at"] or utcnow()
        if job.status in {"new", "seen", "closed", "hidden"}:
            application_tracker.set_status(
                job, "applied", source="email", now=checked["occurred_at"] or utcnow()
            )
        if str(job.applied_confidence or "") not in {"portal", "receipt"}:
            # User-provided .eml is a useful manual confirmation, but editable
            # Authentication-Results cannot earn platform trust by itself.
            job.applied_confidence = "manual"
        session.add(job)
        applications.record_submitted_in_session(session, [job])
        try:
            session.commit()
            session.refresh(evidence)
        except Exception:
            if target is not None:
                target.unlink(missing_ok=True)
            raise

    return evidence


def existing_map(session, jobs) -> dict[tuple[str, str], dict]:
    """Последнее живое письмо по каждой вакансии, без чтения его содержимого."""
    keys = {
        (str(getattr(job, "source", None) or "salling"), str(getattr(job, "id", "")))
        for job in jobs or [] if getattr(job, "id", None)
    }
    if not keys:
        return {}
    ids = {job_id for _source, job_id in keys}
    rows = session.exec(select(ApplicationEvidence).where(
        ApplicationEvidence.kind == "email",
        ApplicationEvidence.job_id.in_(ids),
    ).order_by(ApplicationEvidence.created_at.desc())).all()
    result: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (str(row.source), str(row.job_id))
        path = EMAIL_DIR / Path(str(row.path or "")).name
        if key not in keys or key in result or not _artifact_matches(row, path):
            continue
        result[key] = {
            "file": path.name,
            "sender": str(row.sender or ""),
            "subject": str(row.subject or ""),
            "authentication": str(row.authentication or ""),
            "occurred_at": row.occurred_at,
        }
    return result


def valid_rows(session, source: str) -> list[ApplicationEvidence]:
    """Cryptographically/provider-verified email rows only.

    Header-only imports use ``unverified_header`` and deliberately do not
    appear here.  Future DKIM/OAuth verification may store one of the explicit
    verified values below.
    """
    rows = session.exec(select(ApplicationEvidence).where(
        ApplicationEvidence.source == str(source or ""),
        ApplicationEvidence.kind == "email",
    )).all()
    return [
        row for row in rows
        if row.authentication in {"dkim_verified", "provider_verified"}
        and _artifact_matches(row, EMAIL_DIR / Path(str(row.path or "")).name)
    ]


def _artifact_matches(row: ApplicationEvidence, path: Path) -> bool:
    """Артефакт остаётся доказательством только пока совпадает сохранённый hash."""
    expected = str(row.fingerprint or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or not path.is_file():
        return False
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return False
    return digest == expected
