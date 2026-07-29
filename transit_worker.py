"""Фоновый расчёт времени в пути (дом → вакансия) для карточки в телефоне.

Зачем: в списке показывалось расстояние ПО ПРЯМОЙ, и «≈1 км» вводило в
заблуждение — реальный путь может идти в обход озера и занимать 20 минут.
Настоящее время в пути считает Transitous (бесплатно, без ключа), но он
медленный и сериализует запросы по IP, поэтому:

  * считаем ТОЛЬКО подходящие вакансии, ближние первыми;
  * один запрос за проход, с паузой между ними;
  * результат кэшируется на диск навсегда (transit.summary), поэтому повторно
    он бесплатный и мгновенный;
  * при серии сбоев уходим в длинную паузу.

Воркер ничего не решает и ничего не подаёт — только заполняет кэш маршрутов.
Отбор кандидатов — чистая функция, покрыта тестами.
"""
from __future__ import annotations

import threading
import time

import transit

IDLE_SLEEP = 300.0      # считать нечего — редко проверяем
STEP_SLEEP = 20.0       # пауза между запросами (Transitous не любит частые)
BACKOFF_SLEEP = 900.0   # после серии сбоев сети — длинная пауза
MAX_FAILS = 3
SYNC_EVERY_SEC = 300.0  # как часто подталкивать синк списка в облако

_stop = threading.Event()
_thread = None


def needs_transit(job, home) -> bool:
    """Нужно ли считать маршрут: есть дом, есть координаты, в кэше пусто."""
    if not home or getattr(job, "lat", None) is None or getattr(job, "lon", None) is None:
        return False
    return transit.cached(home["lat"], home["lon"], job.lat, job.lon) is None


def _straight_km(job, home) -> float:
    import geo
    try:
        return geo.haversine_km(home["lat"], home["lon"], job.lat, job.lon)
    except Exception:  # noqa: BLE001
        return 10 ** 9


def pick_next(jobs, home):
    """Следующая вакансия на расчёт: ближняя по прямой первой — она с большей
    вероятностью реально доступна, и её время нужнее всего."""
    todo = [j for j in (jobs or []) if needs_transit(j, home)]
    if not todo:
        return None
    todo.sort(key=lambda j: _straight_km(j, home))
    return todo[0]


def compute_one(job, home) -> dict:
    """Посчитать и закэшировать маршрут для одной вакансии."""
    return transit.summary(home["lat"], home["lon"], job.lat, job.lon)


def _candidates():
    import autopilot
    return autopilot.find_matches()


def _home():
    import settings_store
    return settings_store.get_home()


def _run(candidates_fn=_candidates, home_fn=_home, compute_fn=compute_one,
         sync_fn=None, busy_fn=None, now_fn=time.monotonic):
    """Рабочий цикл с инъекцией зависимостей — тестируется без сети и БД."""
    fails = 0
    last_sync = 0.0
    while not _stop.is_set():
        try:
            home = home_fn()
            job = pick_next(candidates_fn(), home) if home else None
        except Exception as e:  # noqa: BLE001 — БД занята и т.п.
            print(f"transit-worker: отбор — {e}")
            job = None
        if job is None:
            _stop.wait(IDLE_SLEEP)
            continue
        if busy_fn and busy_fn():        # идёт подача — не мешаем
            _stop.wait(STEP_SLEEP)
            continue
        try:
            res = compute_fn(job, home)
            if res.get("ok"):
                fails = 0
                # Синк списка не чаще раза в 5 минут: каждая отправка — запись в
                # облако, а маршруты приходят по одному.
                if sync_fn and now_fn() - last_sync >= SYNC_EVERY_SEC:
                    last_sync = now_fn()
                    try:
                        sync_fn(force=True)
                    except Exception:  # noqa: BLE001 — синк не роняет воркер
                        pass
            else:
                # «нет маршрута» тоже кэшируется — вакансия больше не выбирается
                fails += 1
            if fails >= MAX_FAILS:
                fails = 0
                _stop.wait(BACKOFF_SLEEP)
            else:
                _stop.wait(STEP_SLEEP)
        except Exception as e:  # noqa: BLE001
            print(f"transit-worker: ошибка — {e}")
            _stop.wait(BACKOFF_SLEEP)


def start(sync_fn=None, busy_fn=None) -> None:
    """Запустить фоновый воркер (идемпотентно)."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(
        target=_run, kwargs={"sync_fn": sync_fn, "busy_fn": busy_fn},
        daemon=True, name="transit-worker")
    _thread.start()


def stop() -> None:
    _stop.set()
