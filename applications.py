"""Реестр заявок (шаг 3 плана): факты подачи живут в SQLite, а не в settings.json.

Раньше «предложено / пропущено / отправляется / подано» хранились четырьмя
списками id в settings.json. Копии расходились между собой и с jobs.db — вся
серия багов потерянных «Поданных» (1.0.67 → 1.0.75 → 1.0.76 → F35) росла отсюда.
Теперь у заявки одна строка в таблице application, а счётчики (за сегодня,
всего, журнал) ВЫЧИСЛЯЮТСЯ из строк, им больше нечего «терять».

Обратная совместимость: ensure_migrated() один раз переносит старые списки из
settings.json в таблицу и помечает это флагом lists_migrated_to_db, чтобы у
существующих пользователей ничего не потерялось при обновлении.
"""
from __future__ import annotations

import datetime as _dt

from db import Application, Job, get_session, init_db, select, utcnow

# Таблица application создаётся в init_db (create_all). Модуль вызывают из
# разных точек входа (приложение, воркер, скрипты) — гарантируем таблицу сами,
# вызов идемпотентный и дешёвый.
init_db()

SOURCE = "salling"

# Состояния, при которых заявку НЕЛЬЗЯ трогать повторно (уже в работе/подана).
ACTIVE_STATES = ("submitting", "submitted")


def _norm_ids(ids) -> list[str]:
    out, seen = [], set()
    for raw in ids or []:
        jid = str(raw or "").strip()
        if jid and jid not in seen:
            seen.add(jid)
            out.append(jid)
    return out


def _get(s, job_id: str):
    return s.exec(
        select(Application).where(
            Application.source == SOURCE, Application.job_id == str(job_id)
        )
    ).first()


def mark_offered(job_id: str) -> None:
    """Карточку предложили пользователю (гейт F27 запоминает это навсегда)."""
    jid = str(job_id or "").strip()
    if not jid:
        return
    with get_session() as s:
        row = _get(s, jid)
        if row is None:
            row = Application(source=SOURCE, job_id=jid, state="offered")
        if row.offered_at is None:
            row.offered_at = utcnow()
        row.updated_at = utcnow()
        s.add(row)
        s.commit()


def mark_skipped(job_id: str) -> None:
    """Пользователь нажал «Пропустить» — больше не предлагать."""
    jid = str(job_id or "").strip()
    if not jid:
        return
    with get_session() as s:
        row = _get(s, jid)
        if row is None:
            row = Application(source=SOURCE, job_id=jid)
        if row.state not in ACTIVE_STATES:   # поданную «пропустить» нельзя
            row.state = "skipped"
        row.updated_at = utcnow()
        s.add(row)
        s.commit()


def mark_submitting(ids, origin: str = "") -> None:
    """Подача запущена (браузер пошёл заполнять форму)."""
    ids = _norm_ids(ids)
    if not ids:
        return
    with get_session() as s:
        for jid in ids:
            row = _get(s, jid)
            if row is None:
                row = Application(source=SOURCE, job_id=jid)
            if row.state != "submitted":     # уже поданную не откатываем
                row.state = "submitting"
            if origin:
                row.origin = origin
            row.updated_at = utcnow()
            s.add(row)
        s.commit()


def mark_failed(ids) -> None:
    """Подача не подтвердилась. Заявка остаётся в реестре как failed:
    к повторному предложению в TG она не вернётся (offered_at сохранён),
    а тихая автоотправка сможет попробовать снова (как и раньше)."""
    ids = _norm_ids(ids)
    if not ids:
        return
    with get_session() as s:
        for jid in ids:
            row = _get(s, jid)
            if row is not None and row.state == "submitting":
                row.state = "failed"
                row.updated_at = utcnow()
                s.add(row)
        s.commit()


def record_submitted(jobs) -> list:
    """Зафиксировать реально поданные (Job-объекты со status=applied).
    Возвращает список НОВЫХ фиксаций (для лога событий). Идемпотентно:
    повторный вызов по той же вакансии ничего не меняет."""
    fresh = []
    with get_session() as s:
        for j in jobs or []:
            if j is None:
                continue
            jid = str(j.id)
            row = _get(s, jid)
            if row is None:
                row = Application(source=SOURCE, job_id=jid)
            if row.state == "submitted":
                continue                     # уже зафиксирована
            row.state = "submitted"
            row.submitted_at = getattr(j, "applied_at", None) or utcnow()
            row.confidence = getattr(j, "applied_confidence", None) or row.confidence
            row.updated_at = utcnow()
            s.add(row)
            fresh.append(j)
        s.commit()
    return fresh


def state_of(job_id: str) -> str:
    """Текущее состояние заявки ('' — записи нет)."""
    with get_session() as s:
        row = _get(s, str(job_id or "").strip())
    return row.state if row else ""


def _ids_where(*conds) -> set:
    with get_session() as s:
        rows = s.exec(select(Application.job_id).where(Application.source == SOURCE, *conds)).all()
    return {str(r) for r in rows}


def submitted_ids() -> set:
    return _ids_where(Application.state == "submitted")


def submitting_ids() -> set:
    return _ids_where(Application.state == "submitting")


def offered_ids() -> set:
    """Все, кому когда-либо предлагали карточку (гейт F27)."""
    return _ids_where(Application.offered_at.is_not(None))


def skipped_ids() -> set:
    return _ids_where(Application.state == "skipped")


def _leading_failed(states) -> int:
    """Сколько ПОДРЯД последних попыток закончились failed (свежие первыми).
    Чистая функция над списком состояний — легко покрыть тестом."""
    n = 0
    for st in states:
        if st != "failed":
            break
        n += 1
    return n


def failure_streak(limit: int = 10) -> int:
    """Сторож деградации (шаг 7): сколько последних попыток подачи ПОДРЯД не
    подтвердились. Попытка = строка реестра, дошедшая до исхода (submitted или
    failed). Несколько failed подряд — вероятно, Salling изменил сайт."""
    with get_session() as s:
        rows = s.exec(select(Application.state).where(
            Application.source == SOURCE,
            Application.state.in_(("submitted", "failed")),
        ).order_by(Application.updated_at.desc()).limit(limit)).all()
    return _leading_failed([str(r) for r in rows])


def submitted_today_count() -> int:
    day_start = _dt.datetime.combine(_dt.date.today(), _dt.time.min)
    with get_session() as s:
        rows = s.exec(select(Application).where(
            Application.source == SOURCE,
            Application.state == "submitted",
            Application.submitted_at.is_not(None),
            Application.submitted_at >= day_start,
        )).all()
    return len(rows)


def submitted_total_count() -> int:
    with get_session() as s:
        rows = s.exec(select(Application).where(
            Application.source == SOURCE, Application.state == "submitted",
        )).all()
    return len(rows)


def submit_log(limit: int = 50) -> list:
    """Журнал автоподач для интерфейса: [{ts, title}], свежие первыми.
    Вычисляется из реестра + названий вакансий — счётчику нечего терять."""
    with get_session() as s:
        rows = s.exec(select(Application).where(
            Application.source == SOURCE, Application.state == "submitted",
        ).order_by(Application.submitted_at.desc()).limit(limit)).all()
        out = []
        for a in rows:
            job = s.get(Job, a.job_id)
            ts = a.submitted_at.strftime("%d.%m %H:%M") if a.submitted_at else ""
            out.append({"ts": ts, "title": (job.title if job else a.job_id)})
    return out


def clear_offers() -> int:
    """Сброс очереди предложений после смены фильтров: забываем только
    НЕрешённые предложения (state=offered). Пропущенные, отправляющиеся и
    поданные не трогаем — их история важнее смены фильтров."""
    removed = 0
    with get_session() as s:
        rows = s.exec(select(Application).where(
            Application.source == SOURCE, Application.state == "offered",
        )).all()
        for row in rows:
            s.delete(row)
            removed += 1
        s.commit()
    return removed


# ── Миграция старых списков из settings.json (однократная) ─────────────────
def ensure_migrated() -> None:
    """Переносит submitted_ids/submitting_ids/tg_offered_ids/tg_skipped из
    settings.json в таблицу application. Идемпотентно: помечает флагом и
    очищает старые списки, чтобы они не разъезжались с реестром."""
    import settings_store

    data = settings_store.load()
    ap = data.get("autopilot") or {}
    if ap.get("lists_migrated_to_db"):
        return

    now = utcnow()
    with get_session() as s:
        def upsert(jid, **fields):
            jid = str(jid or "").strip()
            if not jid:
                return
            row = _get(s, jid)
            if row is None:
                row = Application(source=SOURCE, job_id=jid, origin="migrated")
            for k, v in fields.items():
                setattr(row, k, v)
            row.updated_at = now
            s.add(row)

        # порядок важен: сначала слабые состояния, потом сильные (submitted
        # перекрывает offered/skipped, как и жило в старых списках)
        for jid in ap.get("tg_offered_ids") or []:
            upsert(jid, state="offered", offered_at=now)
        for jid in ap.get("tg_skipped") or []:
            upsert(jid, state="skipped")
        for jid in ap.get("submitting_ids") or []:
            upsert(jid, state="submitting")
        for jid in ap.get("submitted_ids") or []:
            job = s.get(Job, str(jid))
            upsert(jid, state="submitted",
                   submitted_at=(job.applied_at if job and job.applied_at else now),
                   confidence=(job.applied_confidence if job else None))
        s.commit()

    def _m(d):
        rule = d.get("autopilot") or {}
        rule["lists_migrated_to_db"] = True
        # списки больше не источник правды — оставляем пустыми
        for key in ("submitted_ids", "submitting_ids", "tg_offered_ids", "tg_skipped",
                    "submit_log"):
            rule[key] = []
        rule["submit_count_today"] = 0
        d["autopilot"] = rule
    settings_store.mutate(_m)
    print("реестр заявок: перенёс старые списки из settings.json в базу")
