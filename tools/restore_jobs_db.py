"""Восстановление рабочей базы WexFlow, когда SQLite сообщает «malformed».

Что делает:
  1) уводит битые jobs.db / -wal / -shm в сторону (НЕ удаляет — вдруг пригодятся);
  2) ставит на их место последний целый бэкап;
  3) возвращает отметки «подано» по скринам-квитанциям из logs/applied
     (имя файла — ГГГГММДД_ЧЧММСС_<id вакансии>.png, это и есть доказательство);
  4) печатает отчёт: что перенесено, что восстановлено.

Ничего не выдумывает: подача отмечается только там, где есть скрин-квитанция,
и с датой из имени файла. Вакансии (их список) специально не восстанавливаются —
это кэш, приложение наполнит его само при первом же поиске.

Запуск:
    python tools/restore_jobs_db.py            # показать план, ничего не менять
    python tools/restore_jobs_db.py --apply    # выполнить
    python tools/restore_jobs_db.py --apply --dir <папка данных>
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROOF_RE = re.compile(r"^(\d{8})_(\d{6})_(.+)\.png$", re.I)


def default_data_dir() -> Path:
    import os
    return Path(os.environ.get("APPDATA", "")) / "WexFlow" / "salling"


def newest_backup(data_dir: Path) -> Path | None:
    candidates = sorted(
        [p for p in data_dir.glob("jobs.db.backup_*") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def db_is_healthy(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return str(con.execute("PRAGMA integrity_check").fetchone()[0]).lower() == "ok"
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return False


def proofs(data_dir: Path) -> list[tuple[str, datetime]]:
    """Подтверждённые подачи: id вакансии + время из имени скрина."""
    out: list[tuple[str, datetime]] = []
    folder = data_dir / "logs" / "applied"
    if not folder.is_dir():
        return out
    for item in folder.glob("*.png"):
        match = PROOF_RE.match(item.name)
        if not match:
            continue
        day, clock, job_id = match.groups()
        try:
            when = datetime.strptime(day + clock, "%Y%m%d%H%M%S")
        except ValueError:
            continue
        out.append((job_id, when))
    out.sort(key=lambda pair: pair[1])
    return out


def restore_applied(db_path: Path, marks: list[tuple[str, datetime]]) -> tuple[int, int]:
    """Проставить «подано» по скринам. Возвращает (обновлено, добавлено)."""
    updated = created = 0
    con = sqlite3.connect(db_path)
    try:
        info = list(con.execute("PRAGMA table_info(job)"))
        columns = {row[1] for row in info}
        # Обязательные поля без значения по умолчанию: без них INSERT не пройдёт.
        # Даты обязаны быть настоящими: пустая строка в DATETIME ломает чтение
        # (SQLAlchemy не может её разобрать, и страница «Поданные» падает).
        def _blank(sql_type: str, when: datetime):
            kind = str(sql_type or "").upper()
            if kind.startswith(("INT", "BOOL", "NUM", "REAL", "FLOAT")):
                return 0
            if "DATE" in kind or "TIME" in kind:
                return when.strftime("%Y-%m-%d %H:%M:%S")
            return ""

        required_cols = [(row[1], row[2]) for row in info
                         if row[3] and row[4] is None and row[1] != "id"]
        for job_id, when in marks:
            stamp = when.strftime("%Y-%m-%d %H:%M:%S")
            # В имени скрина — номер вакансии с сайта (requisition_id), а ключ
            # строки в базе — внутренний UUID. Ищем по обоим, иначе восстановление
            # добавит дубли вместо того, чтобы отметить существующие записи.
            row = con.execute(
                "SELECT id, status, applied_at FROM job WHERE id = ? OR requisition_id = ?"
                if "requisition_id" in columns else
                "SELECT id, status, applied_at FROM job WHERE id = ? OR id = ?",
                (job_id, job_id),
            ).fetchone()
            if row is None:
                # вакансии в базе нет (старая, уже закрыта) — сохраняем сам факт
                required = {name: _blank(sql_type, when) for name, sql_type in required_cols}
                fields = {"id": job_id, **required, "status": "applied"}
                if "applied_at" in columns:
                    fields["applied_at"] = stamp
                if "applied_confidence" in columns:
                    fields["applied_confidence"] = "receipt"
                if "title" in columns:
                    fields["title"] = f"Заявка {job_id} (восстановлена по квитанции)"
                if "requisition_id" in columns:
                    fields["requisition_id"] = job_id
                if "source" in columns:
                    fields["source"] = "salling"
                names = ", ".join(fields)
                marks_sql = ", ".join("?" for _ in fields)
                con.execute(f"INSERT INTO job ({names}) VALUES ({marks_sql})", tuple(fields.values()))
                created += 1
                continue
            db_id, status, applied_at = row
            if status == "applied" and applied_at:
                continue
            sets = ["status = 'applied'"]
            params: list = []
            if "applied_at" in columns:
                sets.append("applied_at = COALESCE(applied_at, ?)")
                params.append(stamp)
            if "applied_confidence" in columns:
                sets.append("applied_confidence = COALESCE(NULLIF(applied_confidence, ''), 'receipt')")
            params.append(db_id)
            con.execute(f"UPDATE job SET {', '.join(sets)} WHERE id = ?", tuple(params))
            updated += 1
        con.commit()
    finally:
        con.close()
    return updated, created


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="выполнить, а не только показать план")
    parser.add_argument("--dir", default="", help="папка данных WexFlow")
    args = parser.parse_args()

    data_dir = Path(args.dir) if args.dir else default_data_dir()
    db_path = data_dir / "jobs.db"
    print(f"Папка данных: {data_dir}")
    if not data_dir.is_dir():
        print("Папка не найдена — нечего восстанавливать.")
        return 1

    healthy = db_is_healthy(db_path)
    print(f"Текущая база:  {'ЦЕЛАЯ' if healthy else 'БИТАЯ или отсутствует'}")
    if healthy:
        print("Восстановление не требуется. Ничего не трогаю.")
        return 0

    backup = newest_backup(data_dir)
    if backup is None:
        print("Целого бэкапа рядом нет — восстанавливать не из чего.")
        return 1
    print(f"Бэкап:         {backup.name} ({'целый' if db_is_healthy(backup) else 'ТОЖЕ БИТЫЙ'})")
    if not db_is_healthy(backup):
        print("Бэкап тоже повреждён — останавливаюсь, чтобы не сделать хуже.")
        return 1

    marks = proofs(data_dir)
    print(f"Квитанций:     {len(marks)} скринов подтверждённых подач")
    if not args.apply:
        print("\nЭто предпросмотр. Ничего не изменено. Для запуска добавь --apply")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    for name in ("jobs.db", "jobs.db-wal", "jobs.db-shm"):
        source = data_dir / name
        if source.exists():
            target = data_dir / f"_corrupt_{stamp}_{name}"
            shutil.move(str(source), str(target))
            print(f"  убрано в сторону: {target.name}")
    shutil.copy2(backup, db_path)
    print(f"  поставлен бэкап: {backup.name} -> jobs.db")
    # Бэкап мог быть снят в старом откатном режиме журнала. Если оставить его
    # так, приложение (сервер, hub, воркеры) будет писать базу несколькими
    # процессами БЕЗ WAL — именно в таком виде база ломалась 06.07 и 31.07.
    con = sqlite3.connect(db_path)
    try:
        mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        print(f"  режим журнала: {mode}")
    finally:
        con.close()

    updated, created = restore_applied(db_path, marks)
    print(f"  отметок «подано» возвращено: {updated}, добавлено записей: {created}")
    print("\nГотово. Запусти WexFlow — список вакансий он наполнит сам при первом поиске.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
