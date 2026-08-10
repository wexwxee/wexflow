"""Доверие к площадке: первая подача — с подтверждением и квитанцией.

Шаг 3 пересмотра продукта 08.08.2026.

Правило, ради которого всё написано: **автомат включается не раньше, чем
площадка доказала, что подача через неё доходит.** Пока на площадке не было ни
одной подачи с квитанцией И снимком экрана, WexFlow не отправляет там ничего
сам — только готовит анкету и просит человека нажать. После первой доказанной
подачи приложение предлагает включить автомат для ЭТОЙ площадки; включает
человек, а не программа.

Почему по площадкам, а не «вообще». Подача проверена только у Salling; Lidl не
доведён, Teamtailor/Ashby/Greenhouse не проверялись ни разу. Общий тумблер
«я доверяю автоподаче» означал бы, что доверие, заработанное Salling, молча
переносится на непроверенный коннектор — а там подача может «получиться» и без
заявки.

Что считается доказательством (по убыванию силы):
- ``portal``  — заявка найдена в официальном кабинете работодателя. Это второй,
  независимый уровень из плана: подтверждает не WexFlow, а сам работодатель,
  поэтому снимок экрана здесь не требуется;
- ``email``   — исходное письмо ``.eml`` прошло SPF/DKIM/DMARC и совпало с
  выбранной вакансией. WexFlow не читает ящик и хранит файл только локально;
- ``receipt`` — сайт показал квитанцию «ansøgning modtaget». Это НАШЕ
  утверждение о чужой странице, и засчитывается оно только вместе со снимком
  экрана: обещание «доказательство есть» без файла — это обещание, а не
  доказательство.

``indirect`` (форма исчезла, квитанции не было) и ``manual`` (человек отметил
сам) доверия площадке НЕ дают: первое — догадка, второе — вообще не наша работа.

Тумблер «спрашивать всегда» (``always_ask``, по умолчанию ВКЛючён) остаётся
навсегда и главнее всего остального: пока он включён, автоматической отправки
не происходит ни на одной площадке.
"""
from __future__ import annotations

import re

import config
import email_evidence
import labels as labels_mod
import settings_store
from db import Application, Job, get_session, select

SETTINGS_KEY = "trust"

# Площадки, у которых доверие считается отдельно. Порядок — как в интерфейсе.
# Подписи общие для всего приложения (labels.SOURCES): доверие и сторож
# источников должны называть площадку одинаково.
SOURCES = tuple(labels_mod.SOURCES)
LABELS = dict(labels_mod.SOURCES)

# Чем подтверждена подача. Письмо хранится отдельным артефактом в реестре:
# «portal» — сам по себе, «receipt» — вместе со снимком экрана (см. stats).
# «indirect» (форма исчезла) и «manual» (человек отметил сам) — не в счёт.
PROVING_CONFIDENCE = ("receipt", "portal")

_PROOF_NAME = re.compile(r"\d{8}_\d{6}_(.+)\.png$")
_UNSAFE = re.compile(r"[^0-9A-Za-zА-Яа-я._-]+")


def label(source: str) -> str:
    source = str(source or "").strip()
    return LABELS.get(source, source or "—")


# ── Снимки экрана ──────────────────────────────────────────────────────────
def proof_index() -> dict[str, str]:
    """Карта «ключ вакансии → имя файла-снимка» из logs/applied.

    Salling пишет ``YYYYmmdd_HHMMSS_<requisition_id|job.id>.png``, коннекторы —
    то же самое, но с job_id, где двоеточия заменены на подчёркивания. Файлы
    отсортированы по имени = по времени, поэтому последний остаётся самым свежим.
    """
    proofs: dict[str, str] = {}
    try:
        for path in sorted((config.DATA_DIR / "logs" / "applied").glob("*.png")):
            found = _PROOF_NAME.match(path.name)
            if found:
                proofs[found.group(1)] = path.name
    except OSError:
        pass
    return proofs


def proof_for(job, proofs: dict[str, str] | None = None) -> str:
    """Имя файла-снимка этой вакансии («» — снимка нет)."""
    proofs = proof_index() if proofs is None else proofs
    for key in (getattr(job, "requisition_id", "") or "", str(getattr(job, "id", "") or "")):
        key = str(key)
        if not key:
            continue
        if key in proofs:
            return proofs[key]
        safe = _UNSAFE.sub("_", key)          # так пишут снимки коннекторы
        if safe in proofs:
            return proofs[safe]
    return ""


# ── Настройки ──────────────────────────────────────────────────────────────
def _data() -> dict:
    raw = settings_store.load().get(SETTINGS_KEY)
    return raw if isinstance(raw, dict) else {}


def always_ask() -> bool:
    """Спрашивать перед каждой отправкой. По умолчанию ДА."""
    value = _data().get("always_ask")
    return True if value is None else bool(value)


def set_always_ask(enabled: bool) -> bool:
    enabled = bool(enabled)

    def _mutate(data):
        block = data.get(SETTINGS_KEY)
        block = block if isinstance(block, dict) else {}
        block["always_ask"] = enabled
        data[SETTINGS_KEY] = block

    settings_store.mutate(_mutate)
    return enabled


def auto_enabled(source: str) -> bool:
    """Разрешил ли человек автоматическую отправку на этой площадке."""
    autos = _data().get("auto")
    autos = autos if isinstance(autos, dict) else {}
    return bool(autos.get(str(source or "").strip()))


def set_auto(source: str, enabled: bool) -> tuple[bool, str]:
    """Включить/выключить автомат для площадки.

    Включить можно ТОЛЬКО после доказанной подачи: иначе это обещание за
    непроверенный сайт. Возвращает (итоговое состояние, причина отказа).
    """
    source = str(source or "").strip()
    enabled = bool(enabled)
    if enabled and not stats(source)["proven"]:
        return False, ("на этой площадке ещё не было доказанной подачи — "
                       "сначала одна подача с подтверждением")

    def _mutate(data):
        block = data.get(SETTINGS_KEY)
        block = block if isinstance(block, dict) else {}
        autos = block.get("auto")
        autos = autos if isinstance(autos, dict) else {}
        autos[source] = enabled
        block["auto"] = autos
        # Ответ на предложение получен — больше не спрашиваем про эту площадку.
        offered = block.get("offered")
        offered = offered if isinstance(offered, dict) else {}
        offered[source] = True
        block["offered"] = offered
        data[SETTINGS_KEY] = block

    settings_store.mutate(_mutate)
    return enabled, ""


def offer_answered(source: str) -> bool:
    offered = _data().get("offered")
    offered = offered if isinstance(offered, dict) else {}
    return bool(offered.get(str(source or "").strip()))


def dismiss_offer(source: str) -> None:
    """«Пока не надо»: предложение включить автомат больше не показываем."""
    source = str(source or "").strip()

    def _mutate(data):
        block = data.get(SETTINGS_KEY)
        block = block if isinstance(block, dict) else {}
        offered = block.get("offered")
        offered = offered if isinstance(offered, dict) else {}
        offered[source] = True
        block["offered"] = offered
        data[SETTINGS_KEY] = block

    settings_store.mutate(_mutate)


# ── Что площадка доказала ──────────────────────────────────────────────────
def stats(source: str, proofs: dict[str, str] | None = None) -> dict:
    """Факты о площадке из реестра заявок и снимков — не отдельный счётчик.

    Отдельный счётчик «сколько удачных подач» неизбежно разошёлся бы с реестром
    (так уже было с четырьмя копиями правды о подаче), поэтому считаем каждый
    раз по базе: она и есть источник правды.

    proofs — уже прочитанный каталог снимков: экран доверия спрашивает шесть
    площадок подряд, и перечитывать папку шесть раз незачем.
    """
    source = str(source or "").strip()
    proofs = proof_index() if proofs is None else proofs
    with get_session() as session:
        jobs = session.exec(
            select(Job).where(Job.source == source, Job.applied_at.is_not(None))
            .order_by(Job.applied_at.desc())
        ).all()
        failed = session.exec(
            select(Application).where(Application.source == source,
                                      Application.state == "failed")
        ).all()
        submitted = len(jobs)
        proven_at = None
        proven_title = ""
        receipts = 0          # квитанция сайта + наш снимок экрана
        portal = 0            # заявка видна в кабинете работодателя
        emails = 0            # исходное письмо прошло аутентификацию и совпало с вакансией
        without_proof = 0     # квитанция была, а файла-снимка нет
        proven_ids: set[str] = set()
        jobs_by_id = {str(job.id): job for job in jobs}
        for job in jobs:
            confidence = str(job.applied_confidence or "")
            if confidence == "portal":
                portal += 1   # подтверждает сам работодатель — снимок не нужен
            elif confidence == "receipt":
                if proof_for(job, proofs):
                    receipts += 1
                else:
                    without_proof += 1
                    continue
            else:
                continue
            proven_ids.add(str(job.id))
            if proven_at is None or (job.applied_at and job.applied_at > proven_at):
                proven_at = job.applied_at
                proven_title = str(job.title or "")
        email_rows = email_evidence.valid_rows(session, source)
        email_ids: set[str] = set()
        for evidence in email_rows:
            job = jobs_by_id.get(str(evidence.job_id))
            if job is None or str(evidence.job_id) in email_ids:
                continue
            email_ids.add(str(evidence.job_id))
            proven_ids.add(str(evidence.job_id))
            moment = evidence.occurred_at or evidence.created_at or job.applied_at
            if proven_at is None or (moment and moment > proven_at):
                proven_at = moment
                proven_title = str(job.title or "")
        emails = len(email_ids)
        last = jobs[0] if jobs else None
    return {
        "source": source,
        "label": label(source),
        "submitted": submitted,
        "receipts": receipts,
        "portal": portal,
        "emails": emails,
        "proofs": len(proven_ids),
        "receipts_without_proof": without_proof,
        "failed": len(failed),
        "proven": bool(proven_ids),
        "proven_at": proven_at,
        # какая именно вакансия доказала площадку — её и называем человеку,
        # а не последнюю поданную: это разные вакансии
        "proven_title": proven_title,
        "last_at": getattr(last, "applied_at", None),
        "last_title": str(getattr(last, "title", "") or ""),
        "auto": auto_enabled(source),
    }


def all_stats(sources=SOURCES) -> list[dict]:
    proofs = proof_index()
    return [stats(source, proofs) for source in sources]


def auto_allowed(source: str) -> tuple[bool, str]:
    """Можно ли отправлять на этой площадке БЕЗ подтверждения человека.

    Возвращает (можно, причина отказа по-русски). Причина показывается прямо в
    интерфейсе: «автопилот молчит» без объяснения — та самая немота, из-за
    которой человек перестаёт доверять программе.
    """
    source = str(source or "").strip()
    if always_ask():
        return False, "включено «спрашивать всегда» — подтверждаю каждую отправку"
    if not stats(source)["proven"]:
        return False, (f"{label(source)}: ещё не было подачи, доказанной квитанцией, "
                       "письмом или кабинетом — "
                       "первая идёт с подтверждением")
    if not auto_enabled(source):
        return False, f"автомат для площадки «{label(source)}» не включён"
    return True, ""


def pending_offer() -> dict | None:
    """Площадка, которая только что доказала подачу — предложить автомат.

    Показывается ОДИН раз на площадку: ответ (включил или «пока не надо»)
    запоминается, чтобы предложение не превратилось в надоедливый баннер.
    """
    proofs = proof_index()
    for source in SOURCES:
        if auto_enabled(source) or offer_answered(source):
            continue
        row = stats(source, proofs)
        if row["proven"]:
            return row
    return None


def trim_unproven(ids_by_source: dict) -> tuple[dict, list[str]]:
    """Первая подача на площадке идёт ОДНА — чтобы человек увидел квитанцию.

    Пакетная подача на непроверенной площадке — это ставка на то, что сайт
    ведёт себя как мы думаем, сразу на десяти заявках. Отправляем одну и
    объясняем словами, что происходит.
    """
    trimmed: dict = {}
    notes: list[str] = []
    for source, ids in (ids_by_source or {}).items():
        ids = [str(i) for i in (ids or []) if str(i or "").strip()]
        if len(ids) <= 1 or stats(source)["proven"]:
            trimmed[source] = ids
            continue
        trimmed[source] = ids[:1]
        notes.append(
            f"{label(source)}: это первая подача на площадке — отправляю одну "
            f"и покажу квитанцию. Остальные ({len(ids) - 1}) остались в списке."
        )
    return trimmed, notes
