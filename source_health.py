"""Сторож источников: молчащий источник объявляет себя сломанным.

Шаг 6 пересмотра продукта 08.08.2026. Ставится ДО расширения списка площадок:
пока источников пять, поломку ещё можно заметить глазами; когда их станет
пятнадцать, незамеченная поломка превратится в тихую ложь пользователю.

**Зачем.** Вакансии приходят из чужих API, которых нам никто не обещал: Algolia
у Salling, `karriere.lidl.dk/api`, JSON-фиды Teamtailor/Greenhouse/Ashby. Когда
такой API начинает молча отвечать пустотой (или перестаёт отвечать вовсе), в
базе остаются вчерашние вакансии. Лента при этом выглядит совершенно нормально —
и человек подаётся на то, чего уже нет. Это худший вид поломки: тихий.

**Правило.** Источник, который несколько попыток подряд не принёс ни одной
вакансии, сначала объявляется **молчащим** — это только предупреждение, лента
не меняется (ночной сбой чужого сервера не повод прятать вакансии). Если
молчание длится дольше суток, источник объявляется **сломанным**: его вакансии
уходят из ленты, а приложение говорит об этом словами, а не пустым списком.

**Чего сторож НЕ делает — намеренно:**

- *не трогает базу.* Ни одна вакансия не закрывается и не удаляется: сокрытие
  живёт только в запросе ленты. Источник ожил — его вакансии вернулись сами,
  без починки данных. Закрыть чужие вакансии по своей догадке — такая же ложь,
  как показывать мёртвые, только необратимая;
- *не прячет поданные заявки.* «Поданное» — история человека (инцидент с
  `applied_at`), она не исчезает ни от смены настройки, ни от чужой поломки;
- *не решает за человека.* Скрытое честно посчитано и объяснено на странице
  «Состояние»: видно, какой источник молчит, с какого времени и сколько его
  вакансий скрыто.

Состояние переживает перезапуск (обычный JSON в папке данных) — в отличие от
прежних сторожей в памяти процесса, которые забывали всё при каждом запуске и
поэтому не могли отличить «сбой минуту назад» от «молчит третьи сутки».
"""
from __future__ import annotations

import os
import threading
import time

import config
import json_store
import labels

_LOCK = threading.RLock()

# Сколько попыток подряд без единой вакансии считаем молчанием. Скан идёт раз в
# 3 минуты (автопилот включён) или раз в 30 — то есть три попытки это от
# десяти минут до полутора часов. Меньше трёх — обычная сетевая икота.
QUIET_ATTEMPTS = 3

# Сколько молчание должно длиться, чтобы источник считался сломанным и его
# вакансии ушли из ленты. Сутки выбраны сознательно: ночной сбой чужого сервера
# и выключенный на вечер компьютер не должны опустошать ленту.
BROKEN_AFTER_SECONDS = 24 * 3600

_STATES = ("ok", "quiet", "broken")


_DEFAULT_PATH = config.DATA_DIR / "source_health.json"


def path():
    return _DEFAULT_PATH


def _muted() -> bool:
    """Под тестами в НАСТОЯЩЕЕ состояние не пишем.

    Тест синка с подставным коннектором — это не молчание источника. Один такой
    прогон уже оставил teamtailor с четырьмя неудачами подряд, и через сутки
    dev-лента спрятала бы его вакансии из-за теста. Тест, которому сторож нужен
    по делу, подменяет path() на временный файл — тогда запись разрешена.
    """
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) and path() == _DEFAULT_PATH


def _now(now: float | None = None) -> float:
    return float(now if now is not None else time.time())


# Лента спрашивает сторожа на каждый запрос и на каждую вакансию в цикле, а
# состояние — файл на диске. Держим разобранное значение до следующей записи:
# сверяемся по времени изменения файла, как это делает feed с настройками.
_cache: dict = {"stamp": None, "rows": None}


def _stamp():
    try:
        stat = path().stat()
        return (str(path()), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return (str(path()), 0, 0)


def _load() -> dict:
    stamp = _stamp()
    if _cache["rows"] is not None and _cache["stamp"] == stamp:
        return _cache["rows"]
    data = json_store.read_json(path(), default={}, expected_type=dict) or {}
    rows = data.get("sources")
    rows = rows if isinstance(rows, dict) else {}
    _cache.update(stamp=stamp, rows=rows)
    return rows


def _save(rows: dict) -> None:
    json_store.atomic_write_json(path(), {"sources": rows}, indent=2)
    _cache.update(stamp=None, rows=None)  # не ждём mtime — забываем сразу


def _blank(source: str) -> dict:
    return {
        "source": source,
        "last_attempt_at": 0.0,
        "last_ok_at": 0.0,
        "last_hits": None,
        "fail_streak": 0,
        "silent_since": 0.0,
        "last_error": "",
    }


def report(source: str, hits=None, error: str = "", now: float | None = None) -> dict:
    """Записать итог одной попытки обновления источника.

    hits — сколько вакансий источник отдал (None, если попытка не дошла до
    ответа). Пустой ответ считается таким же молчанием, как ошибка: для наших
    источников «ноль вакансий» не бывает нормой, у каждого из них всегда есть
    хотя бы десятки позиций.
    """
    key = str(source or "").strip().lower()
    if not key or _muted():
        return {}
    stamp = _now(now)
    with _LOCK:
        rows = _load()
        row = {**_blank(key), **(rows.get(key) or {})}
        row["source"] = key
        row["last_attempt_at"] = stamp
        got = None if hits is None else max(0, int(hits))
        row["last_hits"] = got
        if got and not error:
            row["last_ok_at"] = stamp
            row["fail_streak"] = 0
            row["silent_since"] = 0.0
            row["last_error"] = ""
        else:
            row["fail_streak"] = int(row.get("fail_streak") or 0) + 1
            row["last_error"] = str(error or "")[:300]
            if not row.get("silent_since"):
                row["silent_since"] = stamp
        rows[key] = row
        _save(rows)
    return dict(row)


def _verdict(row: dict, now: float | None = None) -> str:
    """ok | quiet | broken — по одной записи, без похода в файл."""
    if int(row.get("fail_streak") or 0) < QUIET_ATTEMPTS:
        return "ok"
    stamp = _now(now)
    since = float(row.get("last_ok_at") or 0.0) or float(row.get("silent_since") or 0.0)
    if not since:
        return "quiet"
    return "broken" if stamp - since >= BROKEN_AFTER_SECONDS else "quiet"


def states(now: float | None = None) -> dict[str, dict]:
    """Полная картина по всем источникам, о которых что-то известно."""
    stamp = _now(now)
    result = {}
    for key, raw in _load().items():
        row = {**_blank(key), **(raw or {})}
        row["state"] = _verdict(row, stamp)
        row["silent_seconds"] = (
            max(0.0, stamp - (float(row.get("last_ok_at") or 0.0)
                              or float(row.get("silent_since") or 0.0)))
            if row["state"] != "ok" else 0.0
        )
        result[key] = row
    return result


def state(source: str, now: float | None = None) -> dict:
    key = str(source or "").strip().lower()
    return states(now).get(key, {**_blank(key), "state": "ok", "silent_seconds": 0.0})


def broken(now: float | None = None) -> tuple[str, ...]:
    """Источники, вакансии которых лента больше не показывает."""
    return tuple(sorted(
        key for key, row in states(now).items() if row["state"] == "broken"
    ))


def quiet(now: float | None = None) -> tuple[str, ...]:
    """Источники, которые молчат, но ещё не признаны сломанными."""
    return tuple(sorted(
        key for key, row in states(now).items() if row["state"] == "quiet"
    ))


def hidden_counts(sources=None) -> dict[str, int]:
    """Сколько вакансий скрыто из ленты по каждому сломанному источнику.

    Поданные не считаем: они из ленты и так не пропадают.
    """
    keys = tuple(sources) if sources is not None else broken()
    if not keys:
        return {}
    from db import Job, get_session, select  # локально: сторож не тянет базу зря
    from sqlmodel import func

    counts = {}
    with get_session() as session:
        for key in keys:
            counts[key] = int(session.exec(
                select(func.count()).select_from(Job).where(
                    Job.source == key,
                    Job.status.not_in(["closed", "hidden", "applied"]),
                )
            ).one() or 0)
    return counts


def silence_label(seconds: float) -> str:
    """«3 часа» / «2 дня» — для честной строки о молчании."""
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} ч"
    return f"{hours // 24} дн"


def view(now: float | None = None) -> list[dict]:
    """Строки для страницы «Состояние»: подпись, вердикт, объяснение словами."""
    hidden = hidden_counts()
    rows = []
    for key, row in sorted(states(now).items()):
        verdict = row["state"]
        if verdict == "broken":
            explain = (
                f"Не отдаёт вакансии {silence_label(row['silent_seconds'])}. "
                f"Его вакансии убраны из ленты — подать на них всё равно нельзя. "
                f"Вернётся сам, как только источник снова ответит."
            )
        elif verdict == "quiet":
            explain = (
                f"Молчит {silence_label(row['silent_seconds'])} "
                f"({row['fail_streak']} попытки подряд без вакансий). "
                f"Лента пока не меняется — ждём сутки, прежде чем прятать."
            )
        else:
            explain = ""
        rows.append({
            "source": key,
            "label": labels.source(key),
            "state": verdict,
            "hits": row.get("last_hits"),
            "fail_streak": int(row.get("fail_streak") or 0),
            "last_ok_at": float(row.get("last_ok_at") or 0.0),
            "silent_label": silence_label(row["silent_seconds"]) if verdict != "ok" else "",
            "hidden": int(hidden.get(key, 0)),
            "error": str(row.get("last_error") or ""),
            "explain": explain,
        })
    return rows


def forget(source: str = "") -> None:
    """Забыть накопленное (источник переименовали, ручная проверка, тесты)."""
    with _LOCK:
        if not source:
            _save({})
            return
        rows = _load()
        rows.pop(str(source or "").strip().lower(), None)
        _save(rows)
