"""Письмо работодателя как локальное ручное свидетельство о заявке.

WexFlow не подключается к почтовому ящику и не просит пароль от него. Человек
скачивает исходное письмо в формате ``.eml`` и прикладывает его к конкретному
отклику. Заголовки загруженного файла редактируемы, поэтому такой импорт не
доказывает площадку и не разблокирует тихую автоподачу. Он лишь помогает
человеку восстановить получение заявки и более поздние этапы, когда есть:

* результат SPF/DKIM/DMARC ``pass`` в служебных заголовках;
* однозначная фраза о получении, рассмотрении, интервью, оффере, найме,
  отказе или отзыве заявки;
* связь с выбранной вакансией (ID либо достаточно характерные слова названия);
* разумная дата относительно уже известной подачи.

Исходный файл остаётся только в ``logs/email`` на компьютере.
"""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
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


STAGE_LABELS = {
    "applied": "Заявка получена",
    "reviewing": "На рассмотрении",
    "interview": "Собеседование",
    "offer": "Оффер",
    "hired": "Принят на работу",
    "rejected": "Отказ",
    "withdrawn": "Заявка отозвана",
}

# Order is intentional.  A rejection often says "after your interview", and
# an employment confirmation may repeat the earlier offer.  The decisive
# outcome must therefore win over a less advanced word occurring in the same
# message.  Conversely, "application accepted" means that the application was
# accepted by the ATS, not that the candidate was hired.
_STAGE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("withdrawn", (
        r"\b(?:your\s+)?application\s+(?:has\s+been|was|is)\s+withdrawn\b",
        r"\bwithdrawal\s+(?:of|from)\s+(?:your\s+)?application\b",
        r"\bconfirmation\s+of\s+(?:your\s+)?withdrawal\b",
        r"\b(?:din\s+)?ans(?:ø|o)gning\s+(?:er(?:\s+blevet)?|blev)\s+trukket\s+tilbage\b",
        r"\bdu\s+har\s+trukket\s+(?:din\s+)?ans(?:ø|o)gning\s+tilbage\b",
        r"\bbekr(?:æ|a)ftelse\s+(?:p(?:å|a)\s+)?tilbagetr(?:æ|a)kning\b",
    )),
    ("rejected", (
        r"\b(?:your\s+)?application\s+(?:has\s+been|was|is)\s+rejected\b",
        r"\b(?:you\s+(?:have\s+)?not|you\s+were\s+not)\s+(?:been\s+)?selected\b",
        r"\bnot\s+selected\s+for\b",
        r"\bno\s+longer\s+under\s+consideration\b",
        r"\b(?:will|have\s+decided)\s+not\s+to\s+(?:move|proceed|continue)\b",
        r"\b(?:will|would)\s+not\s+be\s+(?:moving|proceeding|continuing)(?:\s+forward)?\b",
        r"\bwe\s+(?:will|have\s+decided\s+to)\s+not\s+(?:move|proceed|continue)\b",
        r"\bwe\s+(?:have\s+)?(?:chosen|decided)\s+to\s+(?:move|proceed|continue)(?:\s+forward)?\s+with\s+(?:another|other)\b",
        r"\b(?:your\s+)?application\s+(?:has\s+been|was|is)\s+unsuccessful\b",
        r"\b(?:din\s+)?ans(?:ø|o)gning\s+(?:er(?:\s+blevet)?|blev)\s+afvist\b",
        r"\bafslag\s+(?:p(?:å|a)\s+)?(?:din\s+)?ans(?:ø|o)gning\b",
        r"\bdu\s+er\s+ikke\s+(?:blevet\s+)?udvalgt\b",
        r"\bikke\s+(?:l(?:æ|a)ngere\s+)?taget\s+i\s+betragtning\b",
        r"\bvi\s+har\s+(?:desv(?:æ|a)rre\s+)?valgt\s+at\s+g(?:å|a)\s+videre\s+med\s+(?:en\s+)?anden\b",
        r"\bvi\s+har\s+(?:desv(?:æ|a)rre\s+)?besluttet\s+at\s+g(?:å|a)\s+videre\s+med\s+andre\b",
        # Самая частая датская формулировка отказа: «мы выбрали/нашли другого
        # кандидата». Без неё обычное письмо-отказ вообще не распознавалось.
        r"\bvi\s+har\s+(?:desv(?:æ|a)rre\s+)?(?:valgt|fundet)\s+(?:en\s+)?anden\s+kandidat\b",
        r"\bstillingen\s+er\s+(?:desv(?:æ|a)rre\s+)?(?:nu\s+)?besat\b",
    )),
    ("hired", (
        r"\byou\s+have\s+been\s+hired\b",
        r"\bpleased\s+to\s+confirm\s+(?:your\s+)?(?:employment|appointment)\b",
        r"\b(?:welcome|welcoming)\s+you\s+to\s+(?:the\s+)?(?:team|company)\b",
        r"\bwe\s+(?:are|'re)\s+(?:delighted|pleased)\s+to\s+(?:welcome|appoint)\s+you\b",
        r"\bdu\s+er\s+(?:blevet\s+)?ansat\b",
        r"\bbekr(?:æ|a)fte\s+(?:din\s+)?ans(?:æ|a)ttelse\b",
        r"\bvelkommen\s+(?:til|p(?:å|a))\s+holdet\b",
        r"\bvi\s+gl(?:æ|a)der\s+os\s+til\s+at\s+byde\s+dig\s+velkommen\b",
    )),
    ("offer", (
        r"\bwe\s+(?:are|'re)\s+(?:delighted|pleased)\s+to\s+offer\s+you\b",
        r"\bwe\s+would\s+like\s+to\s+offer\s+you\b",
        r"\boffer\s+of\s+employment\b",
        r"\bjob\s+offer\b",
        r"\btilbud\s+om\s+ans(?:æ|a)ttelse\b",
        r"\bans(?:æ|a)ttelsestilbud\b",
        r"\bvi\s+vil\s+gerne\s+tilbyde\s+dig\s+(?:stillingen|jobbet|ans(?:æ|a)ttelse)\b",
    )),
    ("interview", (
        r"\b(?:invite|inviting)\s+you\s+(?:to|for)\s+(?:an?\s+)?interview\b",
        r"\binvitation\s+to\s+(?:an?\s+)?interview\b",
        r"\b(?:schedule|arrange)\s+(?:an?\s+)?interview\b",
        r"\binterview\s+invitation\b",
        r"\b(?:invitere|inviterer)\s+dig\s+til\s+(?:en\s+)?(?:jobsamtale|samtale)\b",
        r"\binvitation\s+til\s+(?:en\s+)?(?:jobsamtale|samtale)\b",
        r"\bindkalde\s+dig\s+til\s+(?:en\s+)?(?:jobsamtale|samtale)\b",
        r"\bvi\s+vil\s+gerne\s+m(?:ø|o)de\s+dig\s+til\s+(?:en\s+)?samtale\b",
    )),
    ("reviewing", (
        r"\b(?:your\s+)?application\s+(?:is|remains)\s+under\s+review\b",
        r"\bwe\s+(?:are|'re)\s+(?:currently\s+)?reviewing\s+(?:your\s+)?application\b",
        r"\b(?:your\s+)?application\s+(?:is|remains)\s+under\s+consideration\b",
        r"\b(?:din\s+)?ans(?:ø|o)gning\s+(?:er|bliver)\s+under\s+behandling\b",
        r"\bvi\s+behandler\s+(?:nu\s+)?(?:din\s+)?ans(?:ø|o)gning\b",
        r"\b(?:din\s+)?ans(?:ø|o)gning\s+(?:er\s+)?i\s+proces\b",
    )),
    ("applied", (
        r"\btak\s+for\s+din\s+ans(?:ø|o)gning\b",
        r"\bvi\s+har\s+modtaget\s+(?:din\s+)?ans(?:ø|o)gning\b",
        r"\bans(?:ø|o)gning(?:en)?\s+(?:er|blev)\s+modtaget\b",
        r"\bbekr(?:æ|a)ftelse\s+(?:p(?:å|a)\s+)?(?:din\s+)?ans(?:ø|o)gning\b",
        r"\bthank\s+you\s+for\s+apply(?:ing|ing\s+for)\b",
        r"\bwe(?:\s+have|'ve)\s+received\s+your\s+application\b",
        r"\byour\s+application\s+(?:has\s+been|was|is)?\s*received\b",
        r"\byour\s+application\s+(?:has\s+been|was|is)\s+(?:successfully\s+)?submitted\b",
        r"\byour\s+application\s+(?:has\s+been|was|is)\s+accepted\b",
        r"\bapplication\s+(?:receipt|confirmation)\b",
        r"\bans(?:ø|o)gning(?:en)?\s+(?:er|blev)\s+accepteret\b",
    )),
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


def classify_stage(text: str) -> dict:
    """Classify one employer message using conservative Danish/English rules.

    This function deliberately does not use an AI provider: an email can
    contain personal data, and a probabilistic answer must not silently move an
    application to a terminal stage.  Patterns are ordered from decisive
    outcomes to weaker progress signals; see ``_STAGE_PATTERNS``.
    """
    normal = unicodedata.normalize("NFKC", str(text or ""))
    normal = re.sub(r"\s+", " ", normal).strip().casefold()
    for stage, patterns in _STAGE_PATTERNS:
        for pattern in patterns:
            match = re.search(pattern, normal, re.IGNORECASE)
            if match:
                return {
                    "stage": stage,
                    "stage_label": STAGE_LABELS[stage],
                    "matched": match.group(0)[:240],
                }
    return {"stage": "", "stage_label": "", "matched": ""}


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
    classification = classify_stage(searchable)
    if not classification["stage"]:
        raise EvidenceError(
            "В письме не найден понятный этап заявки: получение, рассмотрение, "
            "собеседование, оффер, найм, отказ или отзыв."
        )
    if not _matches_job(job, searchable):
        raise EvidenceError(
            "В письме не найден ID или название выбранной вакансии. Прикрепи его к "
            "нужному отклику либо используй письмо, где вакансия указана явно."
        )

    occurred_at = _message_date(message)
    now = utcnow()
    if occurred_at > now + timedelta(days=2):
        raise EvidenceError("Дата письма находится в будущем.")
    if job.applied_at:
        # A receipt belongs close to the submission.  Later stages legitimately
        # arrive weeks or months afterwards, so they only have to be no earlier
        # than the application (with a small timezone/export tolerance).
        if classification["stage"] == "applied":
            if abs(occurred_at - job.applied_at) > timedelta(days=14):
                raise EvidenceError("Дата подтверждения слишком далеко от даты этой подачи.")
        elif occurred_at < job.applied_at - timedelta(days=2):
            raise EvidenceError("Письмо об этапе датировано раньше выбранной подачи.")

    return {
        "sender": sender_address[:240],
        "subject": subject,
        "authentication": authentication,
        "occurred_at": occurred_at,
        "fingerprint": hashlib.sha256(raw).hexdigest(),
        "stage": classification["stage"],
        "stage_label": classification["stage_label"],
        "matched": classification["matched"],
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


def _model_has_field(model, name: str) -> bool:
    fields = getattr(model, "model_fields", None)
    if fields is None:  # Pydantic v1 compatibility for old frozen builds.
        fields = getattr(model, "__fields__", {})
    return name in (fields or {})


def _row_stage(row, fallback: str = "applied") -> str:
    value = str(getattr(row, "stage", "") or "").strip()
    return value if value in STAGE_LABELS else fallback


def _row_stage_label(row, stage: str) -> str:
    return str(getattr(row, "stage_label", "") or STAGE_LABELS.get(stage, stage))


def _record_status_in_session(session, job: Job, checked: dict) -> None:
    """Bridge email evidence to the shared application event tracker.

    ``record_status_in_session`` is the authoritative implementation in new
    builds.  The small fallback keeps source checkouts made before that schema
    migration usable; it never promotes an email above manual confidence and
    avoids moving an already advanced legacy status backwards.
    """
    recorder = getattr(application_tracker, "record_status_in_session", None)
    if callable(recorder):
        recorder(
            session,
            job,
            checked["stage"],
            origin="email",
            occurred_at=checked["occurred_at"],
            raw_label=checked["subject"],
            evidence_fingerprint=checked["fingerprint"],
            event_key=f"email:{checked['fingerprint']}",
        )
        return

    # Compatibility only; the event-backed tracker supersedes this branch.
    legacy_stage = {
        "applied": "applied",
        "reviewing": "applied",
        "interview": "interview",
        "offer": "offer",
        "hired": "offer",
        "rejected": "rejected",
        "withdrawn": "rejected",
    }[checked["stage"]]
    ranks = {"applied": 1, "interview": 2, "offer": 3}
    current = str(getattr(job, "status", "") or "")
    terminal = current in {"rejected"}
    downgrade = (
        current in ranks and legacy_stage in ranks
        and ranks[legacy_stage] < ranks[current]
    )
    if not terminal and not downgrade:
        application_tracker.set_status(
            job, legacy_stage, source="email", now=checked["occurred_at"]
        )


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
                # Repair legacy/partially committed state on a retry.  The
                # shared event tracker deduplicates by evidence fingerprint.
                retry_checked = dict(checked)
                retry_checked["stage"] = _row_stage(existing, checked["stage"])
                retry_checked["stage_label"] = _row_stage_label(
                    existing, retry_checked["stage"]
                )
                retry_checked["subject"] = str(existing.subject or checked["subject"])
                retry_checked["occurred_at"] = existing.occurred_at or checked["occurred_at"]
                _record_status_in_session(session, job, retry_checked)
                if str(job.applied_confidence or "") not in {"portal", "receipt"}:
                    job.applied_confidence = "manual"
                    session.add(job)
                applications.record_submitted_in_session(session, [job])
                session.commit()
                session.refresh(existing)
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

        evidence_kwargs = {
            "source": str(job.source or "salling"),
            "job_id": str(job.id),
            "kind": "email",
            "path": name,
            "fingerprint": checked["fingerprint"],
            "sender": checked["sender"],
            "subject": checked["subject"],
            "authentication": checked["authentication"],
            "occurred_at": checked["occurred_at"],
        }
        if _model_has_field(ApplicationEvidence, "stage"):
            evidence_kwargs["stage"] = checked["stage"]
        if _model_has_field(ApplicationEvidence, "stage_label"):
            evidence_kwargs["stage_label"] = checked["stage_label"]

        committed = False
        try:
            evidence = ApplicationEvidence(**evidence_kwargs)
            session.add(evidence)
            _record_status_in_session(session, job, checked)

            # A later employer response is also evidence that an application
            # existed, but the editable file remains a manual fact and never
            # earns platform trust.
            if job.applied_at is None:
                job.applied_at = checked["occurred_at"] or utcnow()
            if str(job.applied_confidence or "") not in {"portal", "receipt"}:
                job.applied_confidence = "manual"
            session.add(job)
            applications.record_submitted_in_session(session, [job])
            session.commit()
            committed = True
            session.refresh(evidence)
        except Exception:
            # Once SQLite committed, the row/history points at this exact
            # artifact.  A later refresh/read failure must not delete it and
            # leave durable DB evidence dangling; retry will reconcile it by
            # fingerprint.  Cleanup is only safe before commit ownership.
            if target is not None and not committed:
                target.unlink(missing_ok=True)
            raise

    return evidence


def _job_keys(jobs) -> set[tuple[str, str]]:
    return {
        (str(getattr(job, "source", None) or "salling"), str(getattr(job, "id", "")))
        for job in jobs or [] if getattr(job, "id", None)
    }


def _row_view(row: ApplicationEvidence, path: Path) -> dict:
    stage = _row_stage(row)
    return {
        "id": row.id,
        "file": path.name,
        "sender": str(row.sender or ""),
        "subject": str(row.subject or ""),
        "authentication": str(row.authentication or ""),
        "occurred_at": row.occurred_at,
        "created_at": row.created_at,
        "stage": stage,
        "stage_label": _row_stage_label(row, stage),
    }


def history_map(session, jobs) -> dict[tuple[str, str], list[dict]]:
    """All intact local emails for each application, newest event first."""
    keys = _job_keys(jobs)
    if not keys:
        return {}
    ids = {job_id for _source, job_id in keys}
    rows = session.exec(select(ApplicationEvidence).where(
        ApplicationEvidence.kind == "email",
        ApplicationEvidence.job_id.in_(ids),
    )).all()
    rows.sort(
        key=lambda row: (
            row.occurred_at or row.created_at or datetime.min,
            row.created_at or datetime.min,
            int(row.id or 0),
        ),
        reverse=True,
    )
    result: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (str(row.source), str(row.job_id))
        path = EMAIL_DIR / Path(str(row.path or "")).name
        if key not in keys or not _artifact_matches(row, path):
            continue
        result.setdefault(key, []).append(_row_view(row, path))
    return result


def existing_map(session, jobs) -> dict[tuple[str, str], dict]:
    """Latest intact email plus the total email count for each application."""
    history = history_map(session, jobs)
    return {
        key: {**items[0], "count": len(items)}
        for key, items in history.items() if items
    }


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
        # A verified rejection/interview proves a later application event, but
        # it is not a submission receipt and must not unlock platform trust.
        and _row_stage(row) == "applied"
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
