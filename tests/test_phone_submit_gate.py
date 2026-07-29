"""Подача с телефона по карточке из списка — что её пропускает, а что нет.

Список на телефоне повторяет список приложения и живёт по СВОИМ фильтрам
(возраст, уровень, поиск). Фильтры автопилота («Наборы») отвечают на другой
вопрос — что предлагать самим. Раньше подача проверялась по наборам, и
вакансия, которую человек только что видел на экране, отбивалась сообщением
«Карточка больше не подходит под текущие фильтры». Здесь закрепляем: гейт на
подачу проверяет только актуальность вакансии (и гейт F27 «мы её показывали»).

Ни одной настоящей подачи: launcher — заглушка.
"""
import datetime as dt
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autopilot
from db import Job

# Узкий набор автопилота: только København, только частичная занятость.
NARROW_RULE = {"profiles": [{
    "id": "default", "name": "Набор 1", "enabled": True,
    "cities": "København", "employment_type": "partTime", "max_km": "10",
    "category": "salesGeneral", "age": "adult", "max_hours": "30,37",
}]}


def _gate(job, rule=NARROW_RULE):
    """Прогнать одну вакансию через пакетную подачу с телефона."""
    launched: list[list[str]] = []
    with (
        mock.patch.object(autopilot, "_get_job", return_value=job),
        mock.patch.object(autopilot, "get_rule", return_value=rule),
        mock.patch.object(autopilot, "save_rule"),
        mock.patch.object(autopilot, "mark_submitting"),
        mock.patch.object(autopilot, "log_event"),
        mock.patch.object(autopilot.applications, "offered_ids", return_value=set()),
        mock.patch.object(autopilot.applications, "listed_ids", return_value={job.id}),
        mock.patch.object(autopilot.applications, "submitted_ids", return_value=set()),
        mock.patch.object(autopilot.applications, "submitting_ids", return_value=set()),
    ):
        res = autopilot.tg_submit_batch([job.id], launcher=launched.append)
    return res, launched


def test_submit_from_list_ignores_autopilot_filters():
    """Вакансия из другого города и с полной занятостью — всё равно подаётся."""
    job = Job(id="salling-herlev-1", source="salling", title="1. assistent - Herlev",
              city="Herlev", employment_type="fullTime", status="new")
    res, launched = _gate(job)

    assert res["started"] == [job.id], res
    assert launched == [[job.id]]
    assert not res["skipped"]


def test_submit_refuses_closed_vacancy():
    job = Job(id="salling-closed-1", source="salling", title="Butiksassistent",
              city="København", status="closed")
    res, launched = _gate(job)

    assert res["started"] == []
    assert launched == []
    assert res["skipped"][0]["reason"] == "inactive"


def test_submit_refuses_already_applied_vacancy():
    """applied_at — нерушимая правда: второй раз не подаём."""
    job = Job(id="salling-applied-1", source="salling", title="Kassemedarbejder",
              city="København", status="interview", applied_at=dt.datetime(2026, 7, 1))
    res, launched = _gate(job)

    assert res["started"] == []
    assert launched == []
    assert res["skipped"][0]["state"] == "submitted"
    assert autopilot.can_submit(job) is False


def test_autopilot_own_selection_still_respects_filters():
    """Гейт подачи ослаб, но САМ автопилот по-прежнему предлагает только своё."""
    far = Job(id="salling-herlev-2", source="salling", title="1. assistent - Herlev",
              city="Herlev", employment_type="fullTime", status="new")
    assert autopilot.can_submit(far) is True
    assert autopilot._matches(far, NARROW_RULE, None) is False


if __name__ == "__main__":
    test_submit_from_list_ignores_autopilot_filters()
    test_submit_refuses_closed_vacancy()
    test_submit_refuses_already_applied_vacancy()
    test_autopilot_own_selection_still_respects_filters()
    print("ok")
