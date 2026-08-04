"""Portable candidate passport stays useful without leaking local secrets."""
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import candidate_passport as passport


def _profile(tmp_path=None):
    data = {
        "first_name": "Ivan",
        "last_name": "Test",
        "email": "ivan@example.com",
        "phone": "+45 12 34 56 78",
        "address": "Main street 1",
        "zip": "2300",
        "city": "København",
        "country": "Danmark",
        "linkedin": "https://linkedin.com/in/ivan",
        "languages": "English, Danish",
        "experience_years": "3",
        "current_role": "Sales assistant",
        "education": "Bachelor",
        "about": "Reliable retail worker",
        "start_date": "2026-09-01",
        "work_weekends": "yes",
        "work_night": "no",
        "gender": "male",
        "date_of_birth": "2000-01-02",
        "citizenship": "Ukraine",
        "work_permit": "yes",
        "clean_criminal_record": "yes",
        # These must never escape, even with every export option enabled.
        "password": "LEAK-ME-PASSWORD",
        "api_token": "LEAK-ME-TOKEN",
        "telegram_id": "LEAK-ME-TELEGRAM",
        "relevant_health_condition": "LEAK-ME-HEALTH",
        "lidl_newsletter": "yes",
        "lidl_profile_scope": "international",
        "lidl_referral_name": "LEAK-ME-OTHER-PERSON",
        "company_answer_overrides": {
            "lidl": {"answers": {"lidl_newsletter": "yes"}},
        },
    }
    if tmp_path is not None:
        cv = tmp_path / "Ivan resume.pdf"
        cover = tmp_path / "Motivation letter.docx"
        cv.write_bytes(b"%PDF-test-cv")
        cover.write_bytes(b"PK-test-cover")
        data["cv_path"] = str(cv)
        data["cover_letter_path"] = str(cover)
    return data


def test_payload_is_allow_listed_and_sensitive_data_requires_opt_in(tmp_path):
    profile = _profile(tmp_path)
    regular = passport.build_payload(profile, passport.PassportOptions())
    regular_text = json.dumps(regular, ensure_ascii=False)
    assert regular["candidate"]["identity"]["first_name"] == "Ivan"
    assert regular["candidate"]["contact"]["email"] == "ivan@example.com"
    assert regular["candidate"]["questionnaire"]["work_weekends"] == "yes"
    assert "sensitive" not in regular["candidate"]
    for secret in (
        "LEAK-ME-PASSWORD", "LEAK-ME-TOKEN", "LEAK-ME-TELEGRAM",
        "LEAK-ME-HEALTH", "LEAK-ME-OTHER-PERSON", "international",
        str(tmp_path),
    ):
        assert secret not in regular_text

    opted_in = passport.build_payload(
        profile, passport.PassportOptions(include_sensitive=True),
    )
    assert opted_in["candidate"]["sensitive"]["date_of_birth"] == "2000-01-02"
    assert opted_in["candidate"]["sensitive"]["citizenship"] == "Ukraine"
    opted_text = json.dumps(opted_in, ensure_ascii=False)
    assert "LEAK-ME-HEALTH" not in opted_text
    assert "LEAK-ME-PASSWORD" not in opted_text


def test_options_can_remove_contacts_answers_and_documents(tmp_path):
    options = passport.PassportOptions(
        include_contact=False,
        include_answers=False,
        include_sensitive=False,
        include_cv=False,
        include_cover_letter=False,
    )
    payload = passport.build_payload(_profile(tmp_path), options)
    assert "contact" not in payload["candidate"]
    assert "address" not in payload["candidate"]
    assert "questionnaire" not in payload["candidate"]
    assert payload["documents"] == []


def test_zip_contains_markdown_json_instructions_and_selected_documents(tmp_path):
    stamp = datetime(2026, 8, 4, 10, 30, tzinfo=timezone.utc)
    bundle = passport.build_archive(
        _profile(tmp_path),
        passport.PassportOptions(include_sensitive=True),
        vacancy_url="https://jobs.example.com/role?lang=da#ignored",
        generated_at=stamp,
    )
    assert bundle.filename == "WexFlow-Candidate-Passport-20260804.zip"
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        names = archive.namelist()
        assert names[:3] == [
            "candidate-profile.md", "candidate-profile.json", "INSTRUCTIONS.md",
        ]
        assert "documents/CV-Ivan_resume.pdf" in names
        assert "documents/Cover-letter-Motivation_letter.docx" in names
        assert archive.read("documents/CV-Ivan_resume.pdf") == b"%PDF-test-cv"
        payload = json.loads(archive.read("candidate-profile.json"))
        assert payload["vacancy_url"] == "https://jobs.example.com/role?lang=da"
        assert {item["filename"] for item in payload["documents"]} == {
            "documents/CV-Ivan_resume.pdf",
            "documents/Cover-letter-Motivation_letter.docx",
        }
        instructions = archive.read("INSTRUCTIONS.md").decode("utf-8")
        assert "Не проси присылать пароли" in instructions
        assert "явной команды «отправить»" in instructions


def test_copy_text_is_ready_for_an_external_ai_and_ignores_unsafe_url():
    text = passport.build_copy_text(
        _profile(),
        passport.PassportOptions(include_cv=False, include_cover_letter=False),
        vacancy_url="file:///C:/private.txt",
    )
    assert "[ВСТАВЬ ССЫЛКУ НА ВАКАНСИЮ]" in text
    assert "Перед кнопкой финальной отправки" in text
    assert "LEAK-ME-PASSWORD" not in text
    assert "file:///" not in text


def test_http_export_has_private_download_headers(tmp_path):
    with mock.patch.object(app.profile_store, "load_profile", return_value=_profile(tmp_path)):
        response = app.export_candidate_passport(
            "https://jobs.example.com/role", "1", "1", "", "1", "1",
        )
        text_response = app.candidate_passport_text(
            "https://jobs.example.com/role", "1", "1", "", "1", "1",
        )
    assert response.status_code == 200
    assert response.media_type == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    assert "WexFlow-Candidate-Passport" in response.headers["content-disposition"]
    body = json.loads(text_response.body)
    assert body["ok"] is True
    assert "https://jobs.example.com/role" in body["text"]
    assert "documents/CV" not in body["text"]
    assert text_response.headers["cache-control"] == "no-store"


def test_profile_and_apply_by_link_expose_passport_controls():
    account_html = open(
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates", "account.html"),
        encoding="utf-8",
    ).read()
    link_html = open(
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates", "apply_by_link.html"),
        encoding="utf-8",
    ).read()
    assert "cp.card(passport, 'profile-passport')" in account_html
    assert "cp.card(passport, 'link-passport'" in link_html
    assert 'id="applyByLinkUrl"' in link_html
    partial = open(
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates", "_candidate_passport.html"),
        encoding="utf-8",
    ).read()
    assert "/profile/passport/text" in partial
    assert "/profile/passport/export" in partial
    assert "Чувствительные факты" in partial
