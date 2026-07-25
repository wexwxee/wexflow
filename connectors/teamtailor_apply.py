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
            if (el.count() and el.is_visible() and el.is_editable()
                    and not (el.input_value() or "").strip()):
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
    if upload_cv(page, profile):
        filled.append("CV")
    if attach_cover_letter(page, profile):
        filled.append("cover letter")

    # БЕТА (по умолчанию ВЫКЛ): тот же ИИ-слой, что и в generic_apply. Без флага/
    # ключа Gemini — no-op, поведение как раньше. Отправку не жмём.
    ai_details: list[dict] = []
    try:
        from connectors import ai_fill
        ai_details = ai_fill.fill(page, profile, job=None)
        for d in ai_details:
            tag = "черновик" if d.get("kind") == "draft" else "ИИ"
            filled.append(f"{d.get('label')} ({tag})")
        if ai_details:
            print(f"  ИИ дозаполнил: {[d.get('label') for d in ai_details]}")
    except Exception as ai_err:  # noqa: BLE001 — ИИ-слой не должен ломать подачу
        print("  ИИ-дозаполнение пропущено:", str(ai_err)[:120])

    questions = count_questions(page)
    missing = missing_required(page)
    add_banner(page, questions, filled, platform="Teamtailor", missing=missing, ai_details=ai_details)
    print(f"  вопросов вакансии: {questions} | дозаполнить: {missing or '—'}")
    print("  ГОТОВО — НЕ отправляю. Проверь, поставь согласие и отправь сам.")
