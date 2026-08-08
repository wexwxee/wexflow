"""Вердикт «подойдёт без датского и без местного диплома» (шаг 2, 08.08.2026).

Проверяем ровно то, что обещано человеку: правила цитируют текст вакансии, ИИ
судит РОЛИ и один ответ раздаётся всем её вакансиям, «не ясно» из ленты не
исчезает, а расход ИИ ограничен порциями.

PATH настроек подменяется на временный файл — реальный settings.json НЕ трогаем.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel, Session, create_engine, select

import feed
import relevance
import settings_store
from db import Job, RoleVerdict


def _with_temp_settings(body):
    orig = settings_store.PATH
    settings_store.PATH = Path(tempfile.mkdtemp()) / "settings.json"
    feed._forget()
    try:
        body()
    finally:
        settings_store.PATH = orig
        feed._forget()


def _db(jobs):
    """Временная база + подмена get_session во всех участниках."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for job in jobs:
            session.add(job)
        session.commit()
    return engine, (lambda: Session(engine))


def _job(job_id, title, description="", **kwargs):
    data = {"source": "salling", "country": "DK", "status": "new",
            "categories": "salesGeneral", "city": "København"}
    data.update(kwargs)
    return Job(id=job_id, title=title, description=description, **data)


# ── Слой правил: цитата из текста ──────────────────────────────────────────
def test_rules_quote_the_text_they_judged_by():
    job = _job("a", "Souschef", "Du behersker <b>dansk i tale og skrift</b> og er klar.")
    verdict, reason = relevance.by_rules(job)
    assert verdict == relevance.DANISH
    assert "dansk i tale og skrift" in reason
    assert reason.startswith("в тексте")


def test_rules_read_html_entities():
    """Salling шлёт описание с &aring;/&oslash; — без раскрытия правила слепы."""
    job = _job("b", "Barista", "Vi s&oslash;ger dig, der taler flydende dansk.")
    assert relevance.by_rules(job)[0] == relevance.DANISH


def test_explicit_no_danish_wins_over_the_word_dansk():
    job = _job("c", "Lagermedarbejder",
               "Dansk er ikke et krav. Vi taler engelsk i teamet, dansk hjælper.")
    assert relevance.by_rules(job)[0] == relevance.OK


def test_local_diploma_requirement_is_its_own_verdict():
    job = _job("d", "Sygeplejerske", "Du har dansk autorisation som sygeplejerske.")
    assert relevance.by_rules(job)[0] == relevance.DIPLOMA


def test_silent_text_stays_silent():
    job = _job("e", "Morgenopfylder", "Du fylder varer op om morgenen.")
    assert relevance.by_rules(job) is None


# ── Слой ролей: один ответ на много вакансий ───────────────────────────────
def test_role_key_ignores_city_and_numbers():
    a = _job("1", "Butiksassistent under 18 år Kgs. Lyngby", city="Kgs. Lyngby")
    b = _job("2", "Butiksassistent under 18 år København S", city="København S")
    assert relevance.role_key(a) == relevance.role_key(b)
    assert relevance.role_phrase(a) == "butiksassistent under år"


def test_role_key_separates_different_categories():
    a = _job("1", "Assistent", categories="cashier")
    b = _job("2", "Assistent", categories="warehouseGoodsHandling")
    assert relevance.role_key(a) != relevance.role_key(b)


def test_one_ai_answer_covers_every_job_of_that_role():
    def body():
        jobs = [_job(str(i), "Kasseassistent København", city="København")
                for i in range(5)]
        engine, sessions = _db(jobs)
        answer = {"ok": True, "model": "test-model", "data": {"roles": [
            {"n": 1, "verdict": "danish", "reason": "Касса требует общения на датском."},
        ]}}
        with mock.patch.object(relevance, "get_session", sessions), \
                mock.patch.object(feed, "hide_barrier", return_value=True), \
                mock.patch("ai_filters.generate_json", return_value=answer) as call, \
                mock.patch("ai_filters.available", return_value=True):
            report = relevance.judge_roles(max_requests=1)
            relevance.apply_to_jobs()
        assert call.call_count == 1, "пять вакансий одной роли — один запрос к ИИ"
        assert report["judged"] == 1
        with Session(engine) as session:
            rows = session.exec(select(Job)).all()
            assert {j.fit for j in rows} == {"danish"}
            assert all(j.fit_engine == "ai:test-model" for j in rows)
            assert all("датском" in (j.fit_reason or "") for j in rows)

    _with_temp_settings(body)


def test_ai_budget_is_respected():
    def body():
        # 60 разных ролей, бюджет — одна порция по 25
        jobs = [_job(str(i), f"Rolle nummer {chr(97 + i % 26)}{i} ", city="Odense")
                for i in range(60)]
        engine, sessions = _db(jobs)
        answer = {"ok": True, "model": "m", "data": {"roles": [
            {"n": n, "verdict": "unclear", "reason": ""} for n in range(1, 26)
        ]}}
        with mock.patch.object(relevance, "get_session", sessions), \
                mock.patch("ai_filters.generate_json", return_value=answer) as call, \
                mock.patch("ai_filters.available", return_value=True):
            report = relevance.judge_roles(max_requests=1)
        assert call.call_count == 1
        assert report["asked"] <= relevance.ROLE_BATCH

    _with_temp_settings(body)


def test_rules_beat_the_role_verdict():
    """Текст конкретной вакансии сильнее общей оценки роли."""
    def body():
        job = _job("x", "Kasseassistent", "Dansk er ikke et krav hos os.")
        key = relevance.role_key(job)      # до сохранения: потом объект отвяжется
        engine, sessions = _db([job])
        with Session(engine) as session:
            session.add(RoleVerdict(key=key, verdict="danish",
                                    reason="Касса требует датского.", engine="ai:m"))
            session.commit()
        with mock.patch.object(relevance, "get_session", sessions):
            relevance.apply_to_jobs()
        with Session(engine) as session:
            stored = session.get(Job, "x")
        assert stored.fit == relevance.OK
        assert stored.fit_engine == "rules"

    _with_temp_settings(body)


def test_unjudged_and_unclear_stay_in_the_feed():
    def body():
        engine, sessions = _db([
            _job("clear", "Morgenopfylder", fit="ok"),
            _job("mute", "Assistent", fit="unclear"),
            _job("new", "Ny rolle"),                      # ещё не оценивали
            _job("wall", "Souschef", fit="danish"),
            _job("diploma", "Sygeplejerske", fit="diploma"),
        ])
        with Session(engine) as session:
            visible = {j.id for j in session.exec(
                select(Job).where(*feed.visible_clauses())).all()}
        assert visible == {"clear", "mute", "new"}, "скрываем только уверенное «нет»"

    _with_temp_settings(body)


def test_filter_can_be_turned_off():
    def body():
        engine, sessions = _db([_job("wall", "Souschef", fit="danish")])
        feed.set_hide_barrier(False)
        with Session(engine) as session:
            visible = {j.id for j in session.exec(
                select(Job).where(*feed.visible_clauses())).all()}
        assert visible == {"wall"}
        assert feed.hide_barrier() is False

    _with_temp_settings(body)


def test_hiding_is_on_by_default():
    def body():
        assert feed.hide_barrier() is True

    _with_temp_settings(body)


def test_changed_text_is_rejudged():
    """Правила поменялись или текст переписали — вердикт обязан пересчитаться."""
    def body():
        job = _job("j", "Assistent", "Ingen krav.")
        engine, sessions = _db([job])
        with mock.patch.object(relevance, "get_session", sessions):
            relevance.apply_to_jobs()
            with Session(engine) as session:
                stored = session.get(Job, "j")
                assert stored.fit == relevance.UNCLEAR
                stored.description = "Du taler og skriver dansk."
                session.add(stored)
                session.commit()
            relevance.apply_to_jobs()
        with Session(engine) as session:
            assert session.get(Job, "j").fit == relevance.DANISH

    _with_temp_settings(body)


def test_garbage_from_ai_is_dropped_not_stored():
    roles = [{"key": "k", "phrase": "assistent", "category": "", "brand": "",
              "snippet": "", "source": "salling", "count": 1}]
    parsed = relevance._parse_batch({"roles": [
        {"n": 1, "verdict": "весьма вероятно", "reason": "…"},
        {"n": 99, "verdict": "ok", "reason": "вне диапазона"},
        {"verdict": "ok"},
        {"n": 1, "verdict": "ok", "reason": "Склад без общения."},
    ]}, roles)
    assert parsed == {1: ("ok", "Склад без общения.")}


def test_sync_keeps_the_verdict_it_already_paid_for():
    """Обновление источника не стирает fit_*: иначе ИИ судил бы одно и то же
    каждые полчаса, а лента моргала бы разметкой."""
    import connector_sync
    import scraper
    from connectors.base import JobItem

    engine, sessions = _db([])
    with Session(engine) as session:
        session.add(Job(id="tt:demo:1", source="teamtailor", title="Butiksmedarbejder",
                        country="DK", status="new", fit="danish", fit_engine="ai:m",
                        fit_reason="Касса требует датского.", fit_hash="abc"))
        session.commit()
    item = JobItem(source="teamtailor", id="tt:demo:1", title="Butiksmedarbejder",
                   company="Demo ApS", url="https://demo.teamtailor.com/jobs/1",
                   city="København", country="DK")
    connector_sync.sync_items("teamtailor", [item], sessions)
    with Session(engine) as session:
        stored = session.get(Job, "tt:demo:1")
    assert stored.fit == "danish" and stored.fit_engine == "ai:m"

    # у Salling то же правило — список исключений один на оба источника
    fresh = scraper.hit_to_job({"objectID": "x", "title": "Kasseassistent"})
    kept = {"fit", "fit_reason", "fit_engine", "fit_hash", "fit_at"}
    assert kept <= set(fresh.model_dump()), "поля вердикта должны быть в модели"


def test_background_work_never_runs_from_a_test():
    """Фоновые потоки под тестами не запускаются — ни оценка, ни синк в облако.

    Оба случая уже били по нам: оценка ролей увела четыре настоящих запроса с
    ключа Ивана, а фоновый синк дописывал 289 настоящих id в список, который
    проверял совсем другой тест, и тот падал «сам по себе».
    """
    import app

    assert app._background_muted() is True
    with mock.patch("ai_filters.generate_json") as ai, \
            mock.patch.object(app.threading, "Thread") as thread:
        app._start_relevance_worker()
        app._start_view_sync()
        assert ai.call_count == 0
        assert thread.call_count == 0, "фоновый поток не должен стартовать в тесте"


def test_describe_marks_ai_opinion():
    ai_job = _job("a", "Kasse", fit="danish", fit_engine="ai:gemini",
                  fit_reason="Касса требует датского.")
    rules_job = _job("b", "Kasse", fit="danish", fit_engine="rules",
                     fit_reason="в тексте: «flydende dansk»")
    assert relevance.describe(ai_job)["by_ai"] is True
    assert relevance.describe(rules_job)["by_ai"] is False
    assert relevance.describe(ai_job)["barrier"] is True
    assert relevance.describe(_job("c", "X"))["verdict"] == relevance.UNCLEAR


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print("ok")
