"""Ссылка на первоисточник вакансии в «Подробнее».

Описание в WexFlow — снимок, сделанный при сборе. Работодатель мог его
поправить, поэтому человеку нужен способ открыть вакансию своими глазами.

Правила, которые тут закреплены:
- ссылка ведёт только на http/https (в поле лежат данные из чужих фидов);
- нет ссылки — нет и блока, а не пустая кнопка в никуда;
- человеку видно, на какой домен он уходит;
- внешняя вкладка открывается без передачи нашей страницы (noopener).

Запуск:  python tests/test_source_link.py   (или pytest)
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app

TEMPLATE = (
    Path(__file__).resolve().parents[1] / "templates" / "detail.html"
).read_text(encoding="utf-8")


def _job(link):
    return SimpleNamespace(application_link=link)


def test_normal_vacancy_links_are_kept():
    for link in (
        "https://danskespil.teamtailor.com/jobs/8022318-app",
        "http://careers.example.com/job/1",
        "https://ea-lidl.cfapps.eu20.hana.ondemand.com/easyapply/index.html?ReqId=7",
    ):
        assert app._job_source_url(_job(link)) == link


def test_dangerous_and_empty_links_are_dropped():
    """В поле ссылки лежат данные из чужих фидов — кликабельным делаем не всё."""
    for link in ("javascript:alert(1)", "data:text/html,<h1>", "file:///C:/Windows",
                 "careers.example.com/job/1", "", "   ", None):
        assert app._job_source_url(_job(link)) == "", f"не должно пройти: {link!r}"


def test_missing_job_does_not_break_the_page():
    assert app._job_source_url(None) == ""
    assert app._job_source_host(None) == ""


def test_host_is_shown_without_www():
    job = _job("https://www.corporate.trustpilot.com/careers/job/7668074")
    assert app._job_source_host(job) == "corporate.trustpilot.com"


def test_host_is_empty_when_the_link_is_not_shown():
    assert app._job_source_host(_job("javascript:alert(1)")) == ""


def test_template_hides_the_block_without_a_link():
    assert TEMPLATE.count("{% if source_url %}") >= 2, (
        "и кнопка, и подпись у «Оригинала» должны прятаться без ссылки"
    )


def test_external_link_opens_safely():
    for chunk in TEMPLATE.split("{{ source_url }}")[1:]:
        head = chunk[:160]
        assert 'target="_blank"' in head
        assert "noopener" in head, "внешняя вкладка не должна получать доступ к нашей странице"


def test_person_can_see_where_the_link_leads():
    assert "{{ source_host }}" in TEMPLATE
    assert "снимок описания" in TEMPLATE, (
        "надо честно сказать, почему оригинал может отличаться"
    )


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"OK   {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    print("\n" + (f"ВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ" if not failures
                  else f"{failures} ТЕСТ(ОВ) УПАЛО"))
    sys.exit(1 if failures else 0)
