"""Тесты инвариантов автоматической подачи (фиксируют текущее поведение
ПЕРЕД рефакторингом единого сериализатора — шаг 1.1).

Покрыто без обращения к реальным данным:
  A) autopilot._matches — какие вакансии вообще считаются «подходящими»:
     поданные (applied_at / status=applied), закрытые/скрытые и РУКОВОДЯЩИЕ
     никогда не подходят (а значит — не уйдут в авто-подачу);
  B) autopilot.auto_submit_tick — гейтинг тихой автоотправки: только при
     enabled+auto_submit, режим Telegram (tg_approval) её запрещает, и за один
     проход уходит не больше MAX_PER_SCAN и не больше дневного остатка.

Зависимости (БД/настройки) в части B подменяются на месте — реальные
jobs.db и settings.json НЕ читаются и НЕ пишутся.

Запуск:  python tests/test_submit_invariants.py   (или pytest)
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autopilot
from db import Job


# ── A. _matches: кто вообще «подходит» ────────────────────────────────────
def _permissive_rule(**over):
    """Правило без ограничений: get_profiles синтезирует один включённый
    профиль из пустых полей -> подходит любая рядовая активная вакансия."""
    r = dict(autopilot.DEFAULT_RULE)
    r["profiles"] = []
    r.update(over)
    return r


def _job(title="Salgsassistent", status="new", applied_at=None):
    return Job(id="t-" + str(title)[:8], title=title, status=status, applied_at=applied_at)


def test_matches_regular_job_true():
    assert autopilot._matches(_job(), _permissive_rule(), None) is True


def test_matches_applied_at_never_matches():
    j = _job(applied_at=datetime.datetime(2026, 6, 11))
    assert autopilot._matches(j, _permissive_rule(), None) is False


def test_matches_status_applied_excluded():
    assert autopilot._matches(_job(status="applied"), _permissive_rule(), None) is False


def test_matches_closed_and_hidden_excluded():
    assert autopilot._matches(_job(status="closed"), _permissive_rule(), None) is False
    assert autopilot._matches(_job(status="hidden"), _permissive_rule(), None) is False


def test_matches_leadership_never_matches():
    for title in ("Store Manager", "Souschef", "Supervisor", "Shift Leader"):
        assert autopilot._matches(_job(title=title), _permissive_rule(), None) is False, title


def test_matches_requires_enabled_profile():
    r = _permissive_rule(profiles=[{"id": "x", "name": "x", "enabled": False}])
    assert autopilot._matches(_job(), r, None) is False


# ── B. auto_submit_tick: гейтинг тихой автоотправки ───────────────────────
def _rule_on(**over):
    r = dict(autopilot.DEFAULT_RULE)
    r.update({"enabled": True, "auto_submit": True, "tg_approval": False, "daily_limit": 3})
    r.update(over)
    return r


def _run_tick(rule, eligible_count=10, submitted_today=0):
    """Прогнать auto_submit_tick с подменёнными зависимостями (без БД/настроек).
    Возвращает {launched: ids|None, asked_for: сколько запрошено у eligible}."""
    import scheduler
    cap = {"launched": None, "asked_for": None}

    def fake_eligible(n):
        cap["asked_for"] = n
        return [Job(id=f"j{i}", title="Salgsassistent", status="new")
                for i in range(min(n, eligible_count))]

    fakes = {
        "get_rule": lambda: rule,
        "submitted_today": lambda: submitted_today,
        "within_schedule": lambda r=None: True,
        "eligible_for_submit": fake_eligible,
        "mark_submitting": lambda ids: None,
        "clear_submitting": lambda ids: None,
    }
    orig = {k: getattr(autopilot, k) for k in fakes}
    orig_notify = getattr(scheduler, "notify", None)
    for k, v in fakes.items():
        setattr(autopilot, k, v)
    scheduler.notify = lambda *a, **k: None
    try:
        autopilot.auto_submit_tick(lambda ids: cap.__setitem__("launched", list(ids)))
    finally:
        for k, v in orig.items():
            setattr(autopilot, k, v)
        scheduler.notify = orig_notify
    return cap


def test_tick_no_submit_when_auto_submit_off():
    assert _run_tick(_rule_on(auto_submit=False))["launched"] is None


def test_tick_no_submit_when_disabled():
    assert _run_tick(_rule_on(enabled=False))["launched"] is None


def test_tick_telegram_mode_blocks_silent_submit():
    # tg_approval (режим «спрашивать в Telegram») главнее тихой автоотправки
    assert _run_tick(_rule_on(tg_approval=True))["launched"] is None


def test_tick_caps_at_max_per_scan():
    cap = _run_tick(_rule_on(daily_limit=100), eligible_count=50, submitted_today=0)
    assert cap["asked_for"] == autopilot.MAX_PER_SCAN
    assert cap["launched"] is not None
    assert len(cap["launched"]) == autopilot.MAX_PER_SCAN


def test_tick_respects_daily_limit_reached():
    assert _run_tick(_rule_on(daily_limit=2), submitted_today=2)["launched"] is None


def test_tick_remaining_below_cap():
    # дневной остаток 1 (< MAX_PER_SCAN) -> просим ровно 1
    cap = _run_tick(_rule_on(daily_limit=1), eligible_count=50, submitted_today=0)
    assert cap["asked_for"] == 1
    assert len(cap["launched"]) == 1


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
