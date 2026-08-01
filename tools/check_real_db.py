"""Проверка НАСТОЯЩЕЙ базы WexFlow — запускается только из Проводника.

Инструменты Claude работают в песочнице с наложенной файловой системой: часть
файлов в %AppData% видна оттуда как смесь настоящих и теневых. Поэтому вердикт
о состоянии базы имеет право выносить только эта проверка, запущенная обычным
двойным кликом, — она видит те же файлы, что и само приложение.

Ничего не меняет: только читает и пишет отчёт в C:\\saling\\db_report.txt
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DATA = Path(os.environ.get("APPDATA", "")) / "WexFlow" / "salling"
DB = DATA / "jobs.db"
REPORT = Path(r"C:\saling\db_report.txt")


def check(path: Path, ignore_journal: bool) -> tuple[bool, str]:
    if not path.exists():
        return False, "файла нет"
    uri = f"file:{path.as_posix()}?mode=ro" + ("&immutable=1" if ignore_journal else "")
    try:
        con = sqlite3.connect(uri, uri=True, timeout=15)
    except Exception as exc:  # noqa: BLE001
        return False, f"не открылась: {str(exc)[:60]}"
    try:
        verdict = str(con.execute("PRAGMA quick_check(1)").fetchone()[0])
        if verdict.lower() != "ok":
            return False, verdict[:80]
        jobs = con.execute("SELECT count(*) FROM job").fetchone()[0]
        apps = con.execute("SELECT count(*) FROM application").fetchone()[0]
        applied = con.execute(
            "SELECT count(*) FROM job WHERE status='applied' OR applied_at IS NOT NULL"
        ).fetchone()[0]
        last = con.execute("SELECT max(last_seen) FROM job").fetchone()[0]
        return True, (f"вакансий {jobs}, заявок в реестре {apps}, "
                      f"поданных {applied}, последняя запись {str(last)[:19]}")
    except Exception as exc:  # noqa: BLE001
        return False, f"чтение таблиц: {str(exc)[:60]}"
    finally:
        con.close()


def main() -> int:
    lines = [f"Проверка базы WexFlow — {datetime.now():%d.%m.%Y %H:%M}",
             f"Папка данных: {DATA}", ""]
    for name in ("jobs.db", "jobs.db-wal", "jobs.db-shm"):
        p = DATA / name
        lines.append(f"  {name:14} "
                     + (f"{p.stat().st_size // 1024} КБ, изменён {datetime.fromtimestamp(p.stat().st_mtime):%d.%m %H:%M}"
                        if p.exists() else "нет"))
    lines.append("")

    whole_ok, whole = check(DB, ignore_journal=False)
    file_ok, only_file = check(DB, ignore_journal=True)
    lines.append(f"База вместе с журналом: {'ЦЕЛА' if whole_ok else 'НЕ ЧИТАЕТСЯ'} — {whole}")
    lines.append(f"Сам файл базы:          {'ЦЕЛ' if file_ok else 'НЕ ЧИТАЕТСЯ'} — {only_file}")
    lines.append("")

    if whole_ok:
        lines.append("ВЫВОД: с базой всё в порядке, чинить нечего. Запускай WexFlow как обычно.")
        code = 0
    elif file_ok:
        lines.append("ВЫВОД: сам файл базы цел, мешает только журнал jobs.db-wal.")
        lines.append("Его можно увести в сторону — данные базы при этом не теряются,")
        lines.append("кроме записей, которые не успели попасть из журнала в базу.")
        code = 2
    else:
        lines.append("ВЫВОД: повреждён сам файл базы — нужна свежая копия из _backups.")
        code = 3

    text = "\n".join(lines)
    print(text)
    try:
        REPORT.write_text(text + "\n", encoding="utf-8")
        print(f"\nОтчёт сохранён: {REPORT}")
    except OSError as exc:
        print(f"\nОтчёт сохранить не удалось: {exc}")
    return code


if __name__ == "__main__":
    sys.exit(main())
