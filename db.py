"""Модель вакансии и доступ к SQLite."""
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
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
    # Текущий этап после подачи и источник последнего изменения. Сам факт
    # подачи определяется applied_at и не исчезает при interview/offer/rejected.
    application_status_updated_at: Optional[datetime] = None
    application_status_source: Optional[str] = None


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


class TransitRoute(SQLModel, table=True):
    """Кэш маршрутов «дом → место работы» (общественный транспорт).

    Ключ — округлённые координаты обеих точек, поэтому все вакансии одного
    магазина делят одну запись. Раньше это лежало в transit_cache.json, который
    переписывался целиком на каждый новый маршрут.

    ok=False — «маршрута нет» или сбой сети: такие перепроверяем чаще удачных.
    """
    key: str = Field(primary_key=True)      # "55.7090,12.4813|55.7105,12.4781"
    ok: bool = True
    minutes: int = 0
    transfers: int = 0
    modes: str = ""                         # "5C, 22" — чем ехать (номера линий)
    kinds: str = ""                         # "bus, train" — вид транспорта тех же линий
    error: str = ""
    updated_at: datetime = Field(default_factory=utcnow)


# timeout=30: ждать освобождения блокировки до 30с, а не падать сразу «database is
# locked». База открыта двумя процессами (приложение + воркер apply.py) и многими
# потоками, поэтому ожидание блокировки критично для надёжной отметки applied (F34).
engine = create_engine(
    f"sqlite:///{config.DB_PATH}", echo=False,
    connect_args={"timeout": 30},
)


journal_mode_warning = ""   # непустая строка = база работает НЕ в режиме WAL


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _rec):
    """WAL + busy_timeout на каждое соединение: читатели не блокируют писателя
    (и наоборот), а запись ждёт занятую базу, а не падает мгновенно.

    Ответ PRAGMA journal_mode ПРОВЕРЯЕМ: переключение молча не срабатывает,
    если базу уже держит другое соединение. Раньше это оставалось незамеченным
    — база продолжала работать в старом откатном журнале, а её при этом писали
    несколько процессов сразу (сервер, hub, воркеры подачи). Именно так
    выглядели поломки 06.07 и 31.07: файл в режиме 1 и размер больше, чем
    записано в его же заголовке — след оборванной записи без WAL.
    """
    global journal_mode_warning
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        mode = str((cur.fetchone() or [""])[0] or "").lower()
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")   # с WAL безопасно и быстрее
        cur.execute("PRAGMA wal_autocheckpoint=400")  # ~1.6 МБ, а не 15-32 МБ
        if mode != "wal":
            journal_mode_warning = (
                f"база открыта в режиме журнала «{mode}», а не WAL — "
                "несколько процессов писать её одновременно не должны"
            )
            print(f"база: ВНИМАНИЕ — {journal_mode_warning}")
        else:
            journal_mode_warning = ""
    finally:
        cur.close()


def checkpoint(truncate: bool = True) -> str:
    """Слить журнал WAL в саму базу и обнулить его.

    Пока этого не происходит, «горячее» состояние живёт в jobs.db-wal: он рос
    до 15-32 МБ, и любая его несовместимость с базой роняла ВСЁ приложение,
    хотя сам файл базы был цел."""
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(
                f"PRAGMA wal_checkpoint({'TRUNCATE' if truncate else 'PASSIVE'})")
        return ""
    except Exception as exc:  # noqa: BLE001 — контрольная точка не критична
        return str(exc)[:200]


def _sqlite_ok(path: Path, ignore_journal: bool = False) -> bool:
    """Читается ли база. ignore_journal=True — смотреть только сам файл,
    не применяя WAL (immutable: без блокировок и без создания файлов)."""
    if not path.exists():
        return False
    uri = f"file:{path.as_posix()}?mode=ro" + ("&immutable=1" if ignore_journal else "")
    try:
        con = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            return str(con.execute("PRAGMA quick_check(1)").fetchone()[0]).lower() == "ok"
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return False


def ensure_healthy_db(db_path: Path | None = None) -> str:
    """Проверить базу ДО работы и починить, если сломался только журнал.

    Разбор поломки 01.08.2026: сам jobs.db был полностью цел (5787 вакансий,
    1231 заявка), а «database disk image is malformed» давал журнал WAL,
    разошедшийся с базой. Приложение при этом не запускалось вовсе. Теперь
    такой журнал уводится в сторону, и WexFlow продолжает работать на данных
    последней контрольной точки вместо полного отказа.

    Возвращает описание того, что сделано (пустая строка — всё было в порядке).
    """
    path = Path(db_path or config.DB_PATH)
    if not path.exists() or _sqlite_ok(path):
        return ""
    if not _sqlite_ok(path, ignore_journal=True):
        return restore_from_backup(path)     # сам файл битый — только копия спасёт
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    moved = []
    for suffix in ("-wal", "-shm"):
        journal = path.with_name(path.name + suffix)
        if not journal.exists():
            continue
        try:
            journal.rename(path.with_name(f"_badjournal_{stamp}_{path.name}{suffix}"))
            moved.append(journal.name)
        except OSError:
            # журнал держит другой процесс — чинит тот, кто стартовал первым
            return ""
    if not moved:
        return ""
    note = ("журнал разошёлся с базой и убран в сторону: "
            + ", ".join(moved) + "; данные базы не пострадали")
    print(f"база: {note}")
    return note


def backup_db(keep: int = 5, db_path: Path | None = None) -> Path | None:
    """Копия базы штатным способом SQLite (можно делать на живой базе).

    До 01.08.2026 бэкап был ровно один и от 26 июня: любая поломка стоила
    месяца истории. Держим несколько свежих и удаляем старые."""
    path = Path(db_path or config.DB_PATH)
    if not path.exists():
        return None
    folder = path.parent / "_backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{path.stem}_{datetime.now().strftime('%Y%m%d_%H%M')}.db"
    try:
        src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except Exception as exc:  # noqa: BLE001 — бэкап не должен ронять приложение
        print(f"бэкап базы: не удался — {str(exc)[:150]}")
        target.unlink(missing_ok=True)
        return None
    old = sorted(folder.glob(f"{path.stem}_*.db"), key=lambda p: p.stat().st_mtime)
    for extra in old[:-keep]:
        try:
            extra.unlink()
        except OSError:
            pass
    return target


def newest_backup(db_path: Path | None = None) -> Path | None:
    """Самая свежая целая копия базы (для восстановления)."""
    path = Path(db_path or config.DB_PATH)
    candidates = sorted(
        list((path.parent / "_backups").glob(f"{path.stem}_*.db"))
        + list(path.parent.glob(f"{path.name}.backup_*")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    return next((p for p in candidates if _sqlite_ok(p, ignore_journal=True)), None)


def restore_from_backup(db_path: Path | None = None) -> str:
    """Последняя линия обороны: сама база не читается — ставим свежую копию."""
    path = Path(db_path or config.DB_PATH)
    backup = newest_backup(path)
    if backup is None:
        return "целой копии базы рядом нет"
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    for suffix in ("", "-wal", "-shm"):
        broken = path.with_name(path.name + suffix)
        if broken.exists():
            try:
                broken.rename(path.with_name(f"_corrupt_{stamp}_{path.name}{suffix}"))
            except OSError as exc:
                return f"битую базу не удалось убрать в сторону: {exc}"
    shutil.copy2(backup, path)
    return f"база восстановлена из копии {backup.name}"


last_repair = ""          # что пришлось починить в базе при последнем запуске

# Лечим ДО первого обращения к базе: одно только открытие файла с испорченным
# журналом вкатывает его битые кадры внутрь и добивает целый файл. Проверка
# стоит ~37 мс на базе в 30 МБ, поэтому делаем её при загрузке модуля — в
# каждом процессе (сервер, hub, воркеры), кто успел первым, тот и починил.
try:
    last_repair = ensure_healthy_db()
except Exception as _exc:  # noqa: BLE001 — проверка не должна мешать запуску
    print(f"база: проверку целостности выполнить не удалось — {_exc}")


def init_db():
    global last_repair
    last_repair = ensure_healthy_db() or last_repair
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
            ("application_status_updated_at", "application_status_updated_at DATETIME"),
            ("application_status_source", "application_status_source VARCHAR"),
        ]:
            if name not in cols:
                conn.execute(text(f"ALTER TABLE job ADD COLUMN {ddl}"))
        # вид транспорта у сохранённых маршрутов: у старых записей пусто,
        # интерфейс до пересчёта угадывает его по номеру линии
        troute = {row[1] for row in conn.execute(text("PRAGMA table_info(transitroute)"))}
        if troute and "kinds" not in troute:
            conn.execute(text("ALTER TABLE transitroute ADD COLUMN kinds VARCHAR NOT NULL DEFAULT ''"))
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
