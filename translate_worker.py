"""Фоновый перевод описаний подходящих вакансий (для экрана детали в Mini App).

Панель на телефоне показывает полное описание по-русски. Чтобы оно открывалось
сразу, переводим заранее в фоне и кладём в job.description_ru. Бюджет скромный:
один перевод за проход, паузы между ними, backoff при сбоях переводчика.

Воркер пишет ТОЛЬКО job.description_ru — очередей, офферов, seen_ids и прочего
состояния автопилота НЕ касается. Отбор кандидатов — чистая функция (тесты).
"""
from __future__ import annotations

import threading
import time

import translator
from db import Job, get_session, select

# Тайминги/бюджет (переопределяемы в тестах через _run с инъекцией пауз).
IDLE_SLEEP = 60.0     # очередь пуста — редкий опрос базы
STEP_SLEEP = 4.0      # пауза между переводами (не жечь CPU/лимиты Google)
BACKOFF_SLEEP = 600.0  # после серии ошибок переводчика — длинная пауза
MAX_FAILS = 3

_stop = threading.Event()
_thread = None
_down = False          # переводчик недоступен (серия ошибок) — панель честно скажет


def is_translator_down() -> bool:
    return _down


def needs_translation(job) -> bool:
    """Есть описание и ещё нет русского перевода."""
    return bool(str(getattr(job, "description", "") or "").strip()) and \
        not str(getattr(job, "description_ru", "") or "").strip()


def _fs_ts(job) -> float:
    fs = getattr(job, "first_seen", None)
    if fs is None:
        return 0.0
    try:
        return fs.timestamp()          # datetime
    except AttributeError:
        try:
            return float(fs)           # число (в тестах)
        except (TypeError, ValueError):
            return 0.0


def pick_next(jobs):
    """Следующая вакансия на перевод: новые первыми, с описанием и без перевода.
    Чистая функция — легко покрыть тестом."""
    todo = [j for j in (jobs or []) if needs_translation(j)]
    todo.sort(key=_fs_ts, reverse=True)
    return todo[0] if todo else None


def translate_one(job) -> None:
    """Перевести описание одной вакансии и сохранить в БД (description_ru).
    Бросает translator.TranslationError, если переводчик недоступен."""
    ru_html = translator.translate_to_ru(job.description, title=job.title or "")
    ru_plain = translator._plain_text(ru_html)
    if not ru_plain:
        return
    with get_session() as s:
        fresh = s.get(Job, job.id)
        if fresh is None:
            return
        fresh.description_ru = ru_html
        s.add(fresh)
        s.commit()
    # обновим объект в памяти, чтобы pick_next не выбрал его снова в этом проходе
    try:
        job.description_ru = ru_html
    except Exception:  # noqa: BLE001
        pass


def _candidates():
    """Кандидаты на перевод: подходящие под автопилот вакансии (новые первыми).
    Импорт autopilot внутри — чтобы модуль оставался лёгким для тестов."""
    import autopilot
    return autopilot.find_matches()


def _run(candidates_fn=_candidates, translate_fn=translate_one,
         sync_fn=None, busy_fn=None):
    """Рабочий цикл. Вынесен с инъекцией зависимостей, чтобы тестировать без
    реального переводчика, БД и сети."""
    global _down
    fails = 0
    while not _stop.is_set():
        try:
            job = pick_next(candidates_fn())
        except Exception as e:  # noqa: BLE001 — БД занята и т.п. — не падаем
            print(f"translate-worker: отбор — {e}")
            job = None
        if job is None:
            _stop.wait(IDLE_SLEEP)
            continue
        if busy_fn and busy_fn():        # идёт подача — не мешаем, ждём
            _stop.wait(STEP_SLEEP)
            continue
        try:
            translate_fn(job)
            _down = False
            fails = 0
            if sync_fn:
                try:
                    sync_fn(force=True)
                except Exception:  # noqa: BLE001 — синк не должен ронять воркер
                    pass
            _stop.wait(STEP_SLEEP)
        except translator.TranslationError as e:
            fails += 1
            print(f"translate-worker: переводчик недоступен ({fails}) — {e}")
            if fails >= MAX_FAILS:
                _down = True
                if sync_fn:
                    try:
                        sync_fn(force=True)   # сообщить панели «недоступен»
                    except Exception:  # noqa: BLE001
                        pass
                _stop.wait(BACKOFF_SLEEP)
                fails = 0
            else:
                _stop.wait(STEP_SLEEP)
        except Exception as e:  # noqa: BLE001 — любой сбой перевода не валит цикл
            print(f"translate-worker: ошибка — {e}")
            _stop.wait(STEP_SLEEP)


def start(sync_fn=None, busy_fn=None) -> None:
    """Запустить фоновый воркер (идемпотентно)."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(
        target=_run, kwargs={"sync_fn": sync_fn, "busy_fn": busy_fn},
        daemon=True, name="translate-worker")
    _thread.start()


def stop() -> None:
    _stop.set()
