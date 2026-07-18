"""Модель вакансии и доступ к SQLite."""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import UniqueConstraint, event, text
from sqlmodel import Field, SQLModel, create_engine, Session, select

import config


def utcnow() -> datetime:
    """Текущее время UTC без таймзоны (как datetime.utcnow, но без deprecation).
    Naive UTC — чтобы не ломать сравнения с уже сохранёнными в базе значениями."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Job(SQLModel, table=True):
    id: str = Field(primary_key=True)            # objectID из Algolia
    source: str = Field(default="salling", index=True)  # salling | teamtailor | ...
    title: str = ""
    brand: Optional[str] = None
    categories: Optional[str] = None             # CSV
    region: Optional[str] = None
    city: Optional[str] = None
    street: Optional[str] = None
    zip: Optional[str] = None
    country: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    hours: Optional[str] = None                  # бывает диапазоном: "15-20"
    employment_type: Optional[str] = None        # fullTime / partTime
    job_level: Optional[str] = None
    trainee: bool = False
    unsolicited: bool = False
    pay_rate: Optional[str] = None               # почти всегда None (нет в объявлениях)
    start_date: Optional[str] = None
    published: Optional[str] = None
    created: Optional[str] = None
    modified: Optional[str] = None
    description: Optional[str] = None            # HTML
    description_ru: Optional[str] = None         # HTML, перевод DeepL на русский
    application_link: Optional[str] = None
    requisition_id: Optional[str] = None

    status: str = "new"                          # new | seen | applied | closed | hidden
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)
    applied_at: Optional[datetime] = None
    # Как подтверждена подача (для журнала доверия):
    #   receipt  — сайт показал квитанцию «ansøgning modtaget» (надёжно);
    #   indirect — форма стабильно исчезла, но квитанции не было (вероятно
    #              подано — стоит проверить письмо от Salling);
    #   manual   — пользователь отметил «подано» вручную (WexFlow не отправлял).
    # None — старые записи до появления поля.
    applied_confidence: Optional[str] = None


class Application(SQLModel, table=True):
    """Реестр заявок — единственный источник правды о фактах вокруг подачи.

    Одна строка на (source, job_id). Раньше эти факты жили СПИСКАМИ id в
    settings.json (submitted_ids / submitting_ids / tg_offered_ids / tg_skipped)
    — четыре копии правды расходились и рождали баги класса F35. Теперь состояние
    заявки — одна запись здесь; settings.json хранит только настройки.

    state: offered (карточка предложена, решения нет) | listed (показана списком
           в панели Mini App, без карточки) | skipped (пропустил) |
           submitting (подача запущена) | submitted (подана) | failed (не подтвердилась).
    """
    __table_args__ = (
        UniqueConstraint("source", "job_id", name="uq_application_source_job"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    source: str = Field(default="salling", index=True)   # salling | teamtailor | greenhouse | ashby
    job_id: str = Field(index=True)
    state: str = "offered"
    origin: str = ""                       # autopilot | telegram | batch | manual | migrated
    confidence: Optional[str] = None       # receipt | indirect | manual (для submitted)
    offered_at: Optional[datetime] = None  # когда карточку предлагали (гейт F27)
    submitted_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=utcnow)


# timeout=30: ждать освобождения блокировки до 30с, а не падать сразу «database is
# locked». База открыта двумя процессами (приложение + воркер apply.py) и многими
# потоками, поэтому ожидание блокировки критично для надёжной отметки applied (F34).
engine = create_engine(
    f"sqlite:///{config.DB_PATH}", echo=False,
    connect_args={"timeout": 30},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _rec):
    """WAL + busy_timeout на каждое соединение: читатели не блокируют писателя
    (и наоборот), а запись ждёт занятую базу, а не падает мгновенно."""
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")   # с WAL безопасно и быстрее
    finally:
        cur.close()


def init_db():
    SQLModel.metadata.create_all(engine)
    _migrate()


def _migrate():
    """Лёгкая миграция: добавляет недостающие колонки в существующую таблицу."""
    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(job)"))}
        for name, ddl in [
            ("source", "source VARCHAR NOT NULL DEFAULT 'salling'"),
            ("lat", "lat FLOAT"),
            ("lon", "lon FLOAT"),
            ("description_ru", "description_ru TEXT"),
            ("applied_confidence", "applied_confidence TEXT"),
        ]:
            if name not in cols:
                conn.execute(text(f"ALTER TABLE job ADD COLUMN {ddl}"))
        conn.commit()
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_job_source ON job (source)"))
        _deduplicate_applications(conn)
        # Контракт реестра — ровно одна строка на source + job_id. Закрепляем
        # его в SQLite, чтобы параллельные потоки не создавали две истории.
        # На новой базе UniqueConstraint уже создал sqlite_autoindex; старой
        # базе добавляем именованный индекс только когда эквивалента ещё нет.
        if not _has_unique_application_key(conn):
            conn.execute(text(
                "CREATE UNIQUE INDEX ux_application_source_job "
                "ON application (source, job_id)"
            ))
        conn.commit()


def _has_unique_application_key(conn) -> bool:
    for row in conn.exec_driver_sql("PRAGMA index_list(application)"):
        if not bool(row[2]):
            continue
        name = str(row[1]).replace('"', '""')
        columns = [info[2] for info in conn.exec_driver_sql(
            f'PRAGMA index_info("{name}")'
        )]
        if columns == ["source", "job_id"]:
            return True
    return False


def _deduplicate_applications(conn) -> int:
    """Объединить старые дубли реестра перед включением уникального индекса."""
    rows = list(conn.execute(text(
        "SELECT id, source, job_id, state, origin, confidence, offered_at, "
        "submitted_at, updated_at FROM application ORDER BY id"
    )).mappings())
    groups = {}
    for row in rows:
        groups.setdefault((row["source"], row["job_id"]), []).append(row)

    rank = {
        "listed": 0,
        "offered": 1,
        "skipped": 2,
        "failed": 3,
        "submitting": 4,
        "submitted": 5,
    }
    removed = 0
    for duplicates in groups.values():
        if len(duplicates) < 2:
            continue
        winner = max(duplicates, key=lambda row: (
            rank.get(str(row["state"] or ""), -1),
            str(row["submitted_at"] or row["updated_at"] or ""),
            int(row["id"]),
        ))
        offered = [row["offered_at"] for row in duplicates if row["offered_at"] is not None]
        submitted = [row["submitted_at"] for row in duplicates if row["submitted_at"] is not None]
        updated = [row["updated_at"] for row in duplicates if row["updated_at"] is not None]
        origin = winner["origin"] or next(
            (row["origin"] for row in duplicates if row["origin"]), ""
        )
        confidence = winner["confidence"] or next(
            (row["confidence"] for row in duplicates if row["confidence"]), None
        )
        conn.execute(text(
            "UPDATE application SET state=:state, origin=:origin, confidence=:confidence, "
            "offered_at=:offered_at, submitted_at=:submitted_at, updated_at=:updated_at "
            "WHERE id=:id"
        ), {
            "id": winner["id"],
            "state": winner["state"],
            "origin": origin,
            "confidence": confidence,
            "offered_at": min(offered) if offered else None,
            "submitted_at": max(submitted) if submitted else None,
            "updated_at": max(updated) if updated else winner["updated_at"],
        })
        for row in duplicates:
            if row["id"] != winner["id"]:
                conn.execute(text("DELETE FROM application WHERE id=:id"), {"id": row["id"]})
                removed += 1
    return removed


def get_session() -> Session:
    return Session(engine)
