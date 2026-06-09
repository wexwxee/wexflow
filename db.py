"""Модель вакансии и доступ к SQLite."""
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel, create_engine, Session, select

import config


def utcnow() -> datetime:
    """Текущее время UTC без таймзоны (как datetime.utcnow, но без deprecation).
    Naive UTC — чтобы не ломать сравнения с уже сохранёнными в базе значениями."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Job(SQLModel, table=True):
    id: str = Field(primary_key=True)            # objectID из Algolia
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


engine = create_engine(f"sqlite:///{config.DB_PATH}", echo=False)


def init_db():
    SQLModel.metadata.create_all(engine)
    _migrate()


def _migrate():
    """Лёгкая миграция: добавляет недостающие колонки в существующую таблицу."""
    from sqlalchemy import text
    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(job)"))}
        for name, ddl in [
            ("lat", "lat FLOAT"),
            ("lon", "lon FLOAT"),
            ("description_ru", "description_ru TEXT"),
        ]:
            if name not in cols:
                conn.execute(text(f"ALTER TABLE job ADD COLUMN {ddl}"))
        conn.commit()


def get_session() -> Session:
    return Session(engine)
