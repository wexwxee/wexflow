"""Тесты «спокойного Telegram» (задача А, июль 2026).

Три предохранителя, чтобы чат не превращался во второй почтовый ящик:
  1. дефолтный радиус: набор без указания места ищет в DEFAULT_HOME_RADIUS_KM
     от дома, а не по всей Дании («вся Дания» — только явный выбор max_km="all");
  2. дневной потолок карточек: tg_sent_today/tg_daily_remaining с переходом
     через полночь;
  3. протухание очереди: карточки без ответа старше TTL снимаются с ожидания.

settings_store.PATH подменяется на временный файл — реальные данные не трогаются.

Запуск:  python tests/test_tg_calm.py   (или pytest)
"""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import settings_store
import autopilot
from db import Job

HOME = {"lat": 55.68, "lon": 12.57}          # Копенгаген
NEAR = {"lat": 55.70, "lon": 12.50}          # ~5 км от дома
FAR = {"lat": 56.15, "lon": 10.20}           # Орхус, ~160 км


def _with_temp(body):
    orig = settings_store.PATH
    tmpdir = tempfile.mkdtemp()
    settings_store.PATH = Path(tmpdir) / "settings.json"
    try:
        body()
    finally:
        settings_store.PATH = orig


def _profile(**over):
    p = {k: autopilot.DEFAULT_RULE.get(k) for k in autopilot._FILTER_FIELDS}
    p.update(over)
    return p


def _job(**over):
    base = dict(id="j1", title="Kasseassistent", brand="Netto", status="new",
                lat=NEAR["lat"], lon=NEAR["lon"])
    base.update(over)
    return Job(**base)


# ── 1. дефолтный радиус ────────────────────────────────────────────────
def test_default_radius_applies_only_without_location():
    assert autopilot.default_radius_applies(_profile(), HOME)
    assert not autopilot.default_radius_applies(_profile(), None)          # дом не задан
    assert not autopilot.default_radius_applies(_profile(max_km="10"), HOME)
    assert not autopilot.default_radius_applies(_profile(max_km="all"), HOME)
    assert not autopilot.default_radius_applies(_profile(cities="Aarhus"), HOME)
    assert not autopilot.default_radius_applies(_profile(regions="Sjælland"), HOME)


def test_far_job_rejected_by_default_radius():
    assert not autopilot._profile_matches(_job(**FAR), _profile(), HOME)


def test_near_job_passes_default_radius():
    assert autopilot._profile_matches(_job(), _profile(), HOME)


def test_explicit_all_denmark_disables_default_radius():
    assert autopilot._profile_matches(_job(**FAR), _profile(max_km="all"), HOME)


def test_default_radius_is_soft_for_missing_coords():
    # без координат под мягким дефолтом НЕ отбрасываем…
    assert autopilot._profile_matches(_job(lat=None, lon=None), _profile(), HOME)
    # …а под явным радиусом — отбрасываем (прежнее строгое поведение)
    assert not autopilot._profile_matches(_job(lat=None, lon=None), _profile(max_km="10"), HOME)


def test_no_home_means_no_radius_at_all():
    assert autopilot._profile_matches(_job(**FAR), _profile(), None)


# ── 2. дневной потолок карточек ────────────────────────────────────────
def test_daily_counter_counts_and_limits():
    def body():
        assert autopilot.tg_sent_today() == 0
        assert autopilot.tg_daily_remaining() == autopilot.TG_DAILY_MAX
        autopilot.tg_note_sent(3)
        autopilot.tg_note_sent(2)
        assert autopilot.tg_sent_today() == 5
        assert autopilot.tg_daily_remaining() == autopilot.TG_DAILY_MAX - 5
        autopilot.tg_note_sent(1000)
        assert autopilot.tg_daily_remaining() == 0
    _with_temp(body)


def test_daily_counter_resets_next_day():
    def body():
        autopilot.tg_note_sent(7)
        # «вчерашний» день в хранилище → счётчик сегодняшнего дня равен нулю
        autopilot.save_rule({"tg_day": "2020-01-01"})
        assert autopilot.tg_sent_today() == 0
        assert autopilot.tg_daily_remaining() == autopilot.TG_DAILY_MAX
    _with_temp(body)


def test_cap_logged_once_per_day():
    def body():
        autopilot.tg_log_cap_once(waiting=9)
        autopilot.tg_log_cap_once(waiting=9)
        events = [e for e in autopilot.event_log() if "потолок" in e.get("text", "")]
        assert len(events) == 1, f"ожидалась одна запись, а их {len(events)}"
    _with_temp(body)


# ── 3. протухание карточек без ответа ──────────────────────────────────
def test_pending_expire_drops_only_old_entries():
    def body():
        old = (dt.datetime.now() - dt.timedelta(days=4)).isoformat(timespec="seconds")
        fresh = dt.datetime.now().isoformat(timespec="seconds")
        autopilot.save_rule({"tg_pending": [
            {"job_id": "a", "message_id": 1, "ts": old},
            {"job_id": "b", "message_id": 2, "ts": fresh},
            {"job_id": "c", "message_id": 3, "ts": "мусор"},   # битую метку не трогаем
        ]})
        dropped = autopilot.tg_pending_expire(days=3)
        assert dropped == 1
        left = {p["job_id"] for p in autopilot.get_rule()["tg_pending"]}
        assert left == {"b", "c"}
    _with_temp(body)


def test_pending_expire_empty_queue_is_noop():
    def body():
        assert autopilot.tg_pending_expire() == 0
    _with_temp(body)


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
