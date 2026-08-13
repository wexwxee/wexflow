"""Письмо, пересланное боту в Telegram, → этап нужного отклика.

Зачем так. Работодатели в Дании чаще отвечают письмом, чем меняют статус в
кабинете, а почтовый ящик WexFlow не читает и читать не будет: для этого нужен
либо пароль от почты (хранить нельзя), либо отдельное OAuth-приложение с
проверкой Google. Пересылка снимает вопрос целиком — человек сам решает, какое
письмо показать приложению, а ящик остаётся нетронутым.

Что здесь происходит и чего НЕ происходит:

- этап определяется тем же ``email_evidence.classify_stage``, что и у файлов
  ``.eml`` — одна формулировка на оба пути;
- вакансия ищется тем же ``email_evidence._matches_job``: по номеру заявки или
  по словам названия, и ТОЛЬКО среди уже поданных откликов. Письмо про вакансию,
  на которую человек не подавался, ничего не меняет;
- совпало несколько или ни одной — честно возвращаем это, а не выбираем
  «наиболее похожую». Угаданный не тот отклик хуже, чем отсутствие записи;
- пересланный текст остаётся **ручным свидетельством**: заголовков письма в нём
  нет, подделать его тривиально, поэтому доверие площадке он не поднимает
  (см. trust.py) и авто-подачу не открывает. Он двигает этап и пишется в
  историю — этого человек и ждёт.
"""
from __future__ import annotations

import hashlib

import application_tracker
import email_evidence
from db import Job, get_session, select

MAX_TEXT = 8000
MIN_TEXT = 20


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())[:MAX_TEXT]


def _applied_jobs(session) -> list[Job]:
    """Только поданные отклики: письмо не может создать заявку из ничего."""
    return list(session.exec(select(Job).where(Job.applied_at.is_not(None))).all())


def _fingerprint(text: str) -> str:
    return hashlib.sha256(_clean(text).encode("utf-8", "replace")).hexdigest()


def analyse_text(text: str) -> dict:
    """Что видно в пересланном тексте: этап и подходящие отклики."""
    clean = _clean(text)
    if len(clean) < MIN_TEXT:
        return {"ok": False, "reason": "short", "stage": "", "matches": []}
    stage = email_evidence.classify_stage(clean)
    with get_session() as session:
        matches = [
            {"id": job.id, "title": str(job.title or ""),
             "brand": str(job.brand or ""), "city": str(job.city or ""),
             "source": str(job.source or "salling")}
            for job in _applied_jobs(session)
            if email_evidence._matches_job(job, clean)
        ]
    return {"ok": True, "stage": stage["stage"], "stage_label": stage["stage_label"],
            "matches": matches, "fingerprint": _fingerprint(clean)}


def import_text(text: str, *, job_id: str = "") -> dict:
    """Привязать пересланное письмо к отклику и подвинуть этап.

    ``job_id`` задаётся, когда человек выбрал заявку сам (совпало несколько).
    Возвращает словарь с полем ``status``: ``saved`` | ``no_match`` |
    ``many`` | ``unknown_stage`` | ``short`` | ``not_applied``.
    """
    seen = analyse_text(text)
    if not seen.get("ok"):
        return {"status": "short"}
    stage = seen["stage"]
    if stage not in application_tracker.STATUS_LABELS:
        # Письмо разобрали, но оно ни о чём не говорит: реклама, рассылка,
        # автоответ «мы получили». Молча ставить «подано» нельзя.
        return {"status": "unknown_stage", "matches": seen["matches"]}

    matches = seen["matches"]
    if job_id:
        matches = [item for item in matches if item["id"] == job_id]
        if not matches:
            return {"status": "not_applied"}
    if not matches:
        return {"status": "no_match", "stage": stage,
                "stage_label": seen.get("stage_label", "")}
    if len(matches) > 1:
        return {"status": "many", "stage": stage,
                "stage_label": seen.get("stage_label", ""), "matches": matches[:6]}

    target = matches[0]
    clean = _clean(text)
    with get_session() as session:
        job = session.get(Job, target["id"])
        if job is None or job.applied_at is None:
            return {"status": "not_applied"}
        result = application_tracker.record_status_in_session(
            session, job, stage,
            origin="email",
            raw_label=clean[:180],
            evidence_fingerprint=seen["fingerprint"],
            # Один и тот же пересланный текст не должен двигать этап дважды.
            event_key=f"forward:{seen['fingerprint']}",
        )
        session.commit()
        title = str(job.title or "")
        brand = str(job.brand or "")
    return {
        "status": "saved",
        "job_id": target["id"], "title": title, "brand": brand,
        "stage": stage, "stage_label": seen.get("stage_label", ""),
        "changed": bool(result.get("changed")),
        "previous": str(result.get("previous_stage") or ""),
    }


def reply_text(result: dict) -> str:
    """Ответ человеку в Telegram — словами, без кодов состояния."""
    status = str(result.get("status") or "")
    if status == "saved":
        where = " · ".join(part for part in (result.get("brand"), result.get("title")) if part)
        head = f"📨 Записал: <b>{result.get('stage_label') or result.get('stage')}</b>"
        if not result.get("changed"):
            head = f"📨 Этап уже стоял: <b>{result.get('stage_label') or result.get('stage')}</b>"
        return (f"{head}\n{where}\n\n"
                "Сохранено в «Моих откликах». Источник — пересланное тобой письмо, "
                "поэтому это ручное свидетельство: подачу оно не подтверждает.")
    if status == "many":
        rows = "\n".join(
            f"• {item.get('brand') or ''} {item.get('title') or ''}".strip()
            for item in result.get("matches") or []
        )
        return ("🤔 Письмо подходит сразу к нескольким откликам, и я не буду "
                f"гадать:\n{rows}\n\nОткрой нужный отклик в приложении и приложи "
                "письмо там — тогда ошибки не будет.")
    if status == "no_match":
        return ("🤔 Не понял, к какой заявке это письмо: ни номера, ни узнаваемого "
                "названия вакансии в тексте нет.\n\nПерешли письмо целиком (вместе "
                "с темой) или приложи его в приложении к нужному отклику.")
    if status == "unknown_stage":
        return ("📄 Письмо принял, но по тексту не видно решения работодателя — "
                "ни приглашения, ни отказа, ни оффера. Ничего не менял.")
    if status == "not_applied":
        return ("🤔 Эта вакансия не значится поданной, поэтому этап менять не стал. "
                "Если подавался вручную — отметь это в приложении.")
    return ("📄 Текста слишком мало, чтобы что-то понять. Перешли письмо целиком, "
            "вместе с темой.")
