"""Время в пути вместо обманчивой «прямой линии».

Скрин Ивана 29.07: карточка обещала «≈1 км», а Google вёл 18 минут на автобусе —
дорога идёт в обход озера. Значит, в телефоне нужно РЕАЛЬНОЕ время в пути.
Проверяем то, что легко сломать молча: отбор кандидатов, бюджет запросов и то,
что уже посчитанное не считается заново.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import transit
import transit_worker


class _Job:
    def __init__(self, jid, lat, lon):
        self.id = jid
        self.lat = lat
        self.lon = lon


HOME = {"lat": 55.7050, "lon": 12.4900}          # Sonnerupvej, Brønshøj


def test_nearest_job_is_computed_first():
    """Ближние по прямой считаем раньше: их время нужнее всего."""
    far = _Job("far", 56.1600, 10.2100)          # Aarhus
    near = _Job("near", 55.7060, 12.4930)        # соседняя улица
    with mock.patch.object(transit, "cached", return_value=None):
        assert transit_worker.pick_next([far, near], HOME).id == "near"


def test_already_cached_job_is_skipped():
    """Готовый маршрут (в том числе «маршрута нет») больше не запрашиваем."""
    job = _Job("j1", 55.7060, 12.4930)
    with mock.patch.object(transit, "cached", return_value={"ok": True, "minutes": 18}):
        assert transit_worker.needs_transit(job, HOME) is False
        assert transit_worker.pick_next([job], HOME) is None
    with mock.patch.object(transit, "cached", return_value={"ok": False, "error": "нет маршрута"}):
        assert transit_worker.needs_transit(job, HOME) is False


def test_job_without_coordinates_is_skipped():
    with mock.patch.object(transit, "cached", return_value=None):
        assert transit_worker.needs_transit(_Job("j2", None, None), HOME) is False
        assert transit_worker.needs_transit(_Job("j3", 55.7, 12.5), None) is False


def test_loop_makes_one_request_per_pass_and_syncs_rarely():
    """Один запрос за проход + синк не чаще раза в SYNC_EVERY_SEC: Transitous
    медленный, а каждый синк — запись в облако."""
    jobs = [_Job(f"j{i}", 55.70 + i / 1000, 12.49) for i in range(4)]
    done = []
    syncs = []
    clock = {"t": 0.0}

    def compute(job, home):
        done.append(job.id)
        if len(done) >= 3:
            transit_worker._stop.set()
        return {"ok": True, "minutes": 10}

    def waiter(seconds):
        clock["t"] += seconds
        return None

    transit_worker._stop.clear()
    with (
        mock.patch.object(transit, "cached", return_value=None),
        mock.patch.object(transit_worker._stop, "wait", side_effect=waiter),
    ):
        transit_worker._run(
            candidates_fn=lambda: jobs,
            home_fn=lambda: HOME,
            compute_fn=compute,
            sync_fn=lambda force=False: syncs.append(clock["t"]),
            now_fn=lambda: clock["t"],
        )
    transit_worker._stop.set()

    # HOME на 55.7050 — ближайшая из ряда 55.700…55.703 это j3; кэш замокан
    # пустым, поэтому она же выбирается каждый проход
    assert done == ["j3", "j3", "j3"]
    assert len(syncs) <= 2, f"синк дёргается слишком часто: {syncs}"


def test_pause_while_applying():
    """Пока идёт подача — маршруты не считаем, чтобы не мешать браузеру."""
    transit_worker._stop.clear()
    calls = []
    waits = []

    def busy():
        waits.append(1)
        if len(waits) >= 3:
            transit_worker._stop.set()
        return True

    with (
        mock.patch.object(transit, "cached", return_value=None),
        mock.patch.object(transit_worker._stop, "wait", side_effect=lambda s: None),
    ):
        transit_worker._run(
            candidates_fn=lambda: [_Job("j1", 55.7, 12.5)],
            home_fn=lambda: HOME,
            compute_fn=lambda job, home: calls.append(job.id) or {"ok": True},
            busy_fn=busy,
        )
    transit_worker._stop.set()
    assert calls == [], "во время подачи маршруты считать нельзя"


def test_screen_jobs_are_computed_before_background():
    """Открытый список важнее фоновой очереди: человек смотрит именно на него."""
    transit_worker._wanted.clear()
    seen = _Job("on-screen", 56.1600, 10.2100)      # далёкая, но на экране
    background = _Job("bg", 55.7060, 12.4930)       # ближняя, но в фоне
    transit_worker.request([seen])
    with mock.patch.object(transit, "cached", return_value=None):
        picked = transit_worker.pick_next(transit_worker._wanted_jobs(), HOME)
        assert picked.id == "on-screen"
        # фоновый список используется только когда срочных не осталось
        transit_worker._wanted.clear()
        assert transit_worker.pick_next(transit_worker._wanted_jobs(), HOME) is None
        assert transit_worker.pick_next([background], HOME).id == "bg"


def test_request_ignores_duplicates_and_jobs_without_coords():
    transit_worker._wanted.clear()
    added = transit_worker.request([
        _Job("a", 55.7, 12.5), _Job("a", 55.7, 12.5), _Job("b", None, None),
    ])
    assert added == 1 and list(transit_worker._wanted) == ["a"]
    transit_worker._forget("a")
    assert transit_worker._wanted == {}


def test_same_address_takes_one_slot():
    """Пять вакансий одного магазина — одна точка в очереди: маршрут у них общий,
    иначе очередь займут дубли вместо пяти РАЗНЫХ адресов."""
    transit_worker._wanted.clear()
    same = [_Job(f"s{i}", 55.7060, 12.4930) for i in range(5)]
    other = _Job("other", 55.6877, 12.4908)
    added = transit_worker.request(same + [other])
    assert added == 2, f"в очередь ушло {added} точек вместо 2"
    transit_worker._wanted.clear()
