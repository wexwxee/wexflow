"""Тесты фонового переводчика описаний (translate_worker).

Проверяем чистую логику отбора и рабочий цикл с инъекцией зависимостей —
без реального переводчика, БД и сети.

Запуск:  python tests/test_translate_worker.py   (или pytest)
"""
import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import translator
import translate_worker


def _job(jid, desc="Dansk tekst", ru="", fs=0.0):
    return SimpleNamespace(id=jid, title="T "+jid, description=desc, description_ru=ru, first_seen=fs)


def test_needs_translation():
    assert translate_worker.needs_translation(_job("a")) is True
    assert translate_worker.needs_translation(_job("b", ru="перевод")) is False
    assert translate_worker.needs_translation(_job("c", desc="")) is False


def test_pick_next_new_first():
    jobs = [_job("old", fs=1.0), _job("new", fs=9.0), _job("mid", fs=5.0)]
    assert translate_worker.pick_next(jobs).id == "new"


def test_pick_next_skips_translated_and_empty():
    jobs = [_job("done", ru="есть", fs=9.0), _job("empty", desc="", fs=8.0), _job("todo", fs=1.0)]
    assert translate_worker.pick_next(jobs).id == "todo"


def test_pick_next_none_when_nothing():
    assert translate_worker.pick_next([_job("x", ru="готово")]) is None
    assert translate_worker.pick_next([]) is None


def test_run_translates_one_then_idles():
    # первый проход переводит «todo», второй — очередь пуста → выходим по _stop
    state = {"jobs": [_job("todo", fs=1.0)], "translated": [], "synced": 0, "waits": []}

    def candidates(): return list(state["jobs"])
    def translate(job): state["translated"].append(job.id); state["jobs"] = []  # больше некого
    def sync(force=False): state["synced"] += 1

    # подменяем _stop.wait, чтобы не спать и остановиться после пустого прохода
    orig_wait = translate_worker._stop.wait
    calls = {"n": 0}
    def fake_wait(t):
        state["waits"].append(t)
        calls["n"] += 1
        if calls["n"] >= 2:   # после idle-паузы — стоп
            translate_worker._stop.set()
        return True
    translate_worker._stop = SimpleNamespace(is_set=lambda: translate_worker.__dict__.get("_stopped", False),
                                             set=lambda: translate_worker.__dict__.__setitem__("_stopped", True),
                                             wait=fake_wait, clear=lambda: None)
    try:
        translate_worker._run(candidates_fn=candidates, translate_fn=translate, sync_fn=sync, busy_fn=None)
    finally:
        translate_worker._stop = threading.Event()
        translate_worker.__dict__.pop("_stopped", None)
    assert state["translated"] == ["todo"]
    assert state["synced"] == 1
    assert translate_worker.STEP_SLEEP in state["waits"]   # пауза между переводами
    assert translate_worker.IDLE_SLEEP in state["waits"]   # затем очередь пуста


def test_run_backoff_marks_down():
    state = {"fails": 0, "down_seen": None}

    def candidates(): return [_job("bad", fs=1.0)]
    def translate(job):
        raise translator.TranslationError("rate limited")
    def sync(force=False):
        state["down_seen"] = translate_worker.is_translator_down()

    calls = {"n": 0}
    def fake_wait(t):
        calls["n"] += 1
        if t == translate_worker.BACKOFF_SLEEP or calls["n"] > 6:
            translate_worker.__dict__["_stopped"] = True
        return True
    translate_worker._down = False
    translate_worker._stop = SimpleNamespace(is_set=lambda: translate_worker.__dict__.get("_stopped", False),
                                             set=lambda: translate_worker.__dict__.__setitem__("_stopped", True),
                                             wait=fake_wait, clear=lambda: None)
    try:
        translate_worker._run(candidates_fn=candidates, translate_fn=translate, sync_fn=sync, busy_fn=None)
    finally:
        translate_worker._stop = threading.Event()
        translate_worker.__dict__.pop("_stopped", None)
        was_down = translate_worker._down
        translate_worker._down = False
    assert was_down is True, "после MAX_FAILS переводчик помечается недоступным"
    assert state["down_seen"] is True, "панели ушёл статус «недоступен»"


def test_run_pauses_while_busy():
    job = _job("todo", fs=1.0)
    state = {"translated": [], "busy": True}

    def candidates(): return [job]
    def translate(j): j.description_ru = "готово"; state["translated"].append(j.id)
    def busy(): return state["busy"]

    calls = {"n": 0}
    def fake_wait(t):
        calls["n"] += 1
        if calls["n"] == 1:
            state["busy"] = False       # после первой паузы «подача» закончилась
        if calls["n"] >= 3:
            translate_worker.__dict__["_stopped"] = True
        return True
    translate_worker._stop = SimpleNamespace(is_set=lambda: translate_worker.__dict__.get("_stopped", False),
                                             set=lambda: translate_worker.__dict__.__setitem__("_stopped", True),
                                             wait=fake_wait, clear=lambda: None)
    try:
        translate_worker._run(candidates_fn=candidates, translate_fn=translate, sync_fn=None, busy_fn=busy)
    finally:
        translate_worker._stop = threading.Event()
        translate_worker.__dict__.pop("_stopped", None)
    assert state["translated"] == ["todo"], "во время подачи ждём, потом переводим"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"OK   {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print("\n" + (f"ВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ" if not failures else f"{failures} ТЕСТ(ОВ) УПАЛО"))
    sys.exit(1 if failures else 0)
