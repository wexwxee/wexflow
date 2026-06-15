"""Точный заполнитель формы Teamtailor (поля известны у всех фирм одинаково).

Открывает форму `{вакансия}/applications/new`, прицельно заполняет
candidate[first_name/last_name/email/phone], грузит CV и ОСТАНАВЛИВАЕТСЯ.
Согласие (GDPR) и «Отправить» — человек. См. fill_common для общих кусков.
"""
from __future__ import annotations

from connectors.fill_common import (
    dismiss_cookies, upload_cv, attach_cover_letter, add_banner, missing_required,
)

FIELD_MAP = {
    'input[name="candidate[first_name]"]': "first_name",
    'input[name="candidate[last_name]"]': "last_name",
    'input[name="candidate[email]"]': "email",
    'input[name="candidate[phone]"]': "phone",
}


def apply_url(job_url: str) -> str:
    u = job_url.rstrip("/")
    return u if u.endswith("/applications/new") else u + "/applications/new"


def fill_fields(page, profile: dict) -> list[str]:
    filled = []
    for selector, key in FIELD_MAP.items():
        val = (profile.get(key) or "").strip()
        if not val:
            continue
        try:
            el = page.locator(selector).first
            if el.count() and el.is_visible():
                el.fill(val)
                filled.append(key)
        except Exception:
            continue
    return filled


def count_questions(page) -> int:
    try:
        return page.locator(
            'input[name*="answers_attributes"][name*="question_id"]'
        ).count()
    except Exception:
        return 0


def prepare(page, job_url: str, profile: dict) -> None:
    url = apply_url(job_url)
    print(f"  открываю форму Teamtailor: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1200)
    dismiss_cookies(page)
    filled = fill_fields(page, profile)
    print(f"  заполнено полей: {filled or '—'}")
    upload_cv(page, profile)
    attach_cover_letter(page, profile)
    questions = count_questions(page)
    missing = missing_required(page)
    add_banner(page, questions, filled, platform="Teamtailor", missing=missing)
    print(f"  вопросов вакансии: {questions} | дозаполнить: {missing or '—'}")
    print("  ГОТОВО — НЕ отправляю. Проверь, поставь согласие и отправь сам.")
