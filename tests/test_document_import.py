"""Bulk AI import: privacy, pairing, preview and confirmed rule creation."""
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import document_import
import document_rules
import app


def _docx_bytes(text: str) -> bytes:
    payload = io.BytesIO()
    escaped = (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "word/document.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{escaped}</w:t></w:r></w:p></w:body></w:document>"
            ),
        )
    return payload.getvalue()


def _uploads():
    return [
        UploadFile(
            filename="Netto_CV.docx",
            file=io.BytesIO(_docx_bytes(
                "Curriculum Vitae. Ivan ivan@example.com +45 12 34 56 78. Netto."
            )),
        ),
        UploadFile(
            filename="Netto_motivation.docx",
            file=io.BytesIO(_docx_bytes(
                "Motivationsbrev til Netto. Kontakt ivan@example.com +45 87 65 43 21."
            )),
        ),
    ]


def test_docx_text_is_extracted_and_sensitive_contacts_are_redacted():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "letter.docx"
        path.write_bytes(_docx_bytes("Netto motivation ivan@example.com +45 12 34 56 78"))
        text = document_import.extract_text(str(path))
        excerpt = document_import._redacted_excerpt(text)

    assert "Netto motivation" in text
    assert "ivan@example.com" not in excerpt
    assert "+45 12 34 56 78" not in excerpt
    assert "[email скрыт]" in excerpt
    assert "[телефон скрыт]" in excerpt


def test_ai_bulk_plan_and_confirmation_create_one_document_rule():
    brands = [{"key": "netto", "label": "Netto", "count": 10}]
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        uploads_dir = root / "uploads"
        settings_path = root / "settings.json"
        ai_result = {
            "ok": True,
            "model": "test-model",
            "data": {
                "groups": [{
                    "target_id": "brand:netto",
                    "cv_id": "f1",
                    "cover_id": "f2",
                    "name": "Netto",
                    "confidence": 0.97,
                    "reason": "Оба файла явно подписаны Netto.",
                }],
                "unassigned_file_ids": [],
            },
        }
        with mock.patch.object(document_import.profile_store, "UPLOAD_DIR", uploads_dir), \
                mock.patch.object(document_import.settings_store, "PATH", settings_path), \
                mock.patch.object(document_import.ai_filters, "available", return_value=True), \
                mock.patch.object(
                    document_import.ai_filters, "generate_json", return_value=ai_result
                ) as generate:
            result = document_import.analyse_uploads(_uploads(), brands, [])
            preview = document_import.get_preview()
            created = document_import.apply_preview(preview, {}, brands, [])
            rules = document_rules.get_rules()
            prompt = generate.call_args.args[0]

    assert result["ok"] is True
    assert preview["model"] == "test-model"
    assert len(preview["groups"]) == 1
    assert len(created) == 1
    assert len(rules) == 1
    assert rules[0]["brand"] == "netto"
    assert Path(rules[0]["cv_path"]).name.startswith("bulk_doc_")
    assert Path(rules[0]["cover_letter_path"]).name.startswith("bulk_doc_")
    assert "ivan@example.com" not in prompt
    assert "+45 12 34 56 78" not in prompt
    assert "[email скрыт]" in prompt


def test_cancelled_preview_removes_only_its_bulk_files():
    brands = [{"key": "netto", "label": "Netto", "count": 10}]
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        uploads_dir = root / "uploads"
        settings_path = root / "settings.json"
        ai_result = {
            "ok": True,
            "data": {
                "groups": [{
                    "target_id": "brand:netto",
                    "cv_id": "f1",
                    "cover_id": "f2",
                    "confidence": 1,
                }],
            },
        }
        with mock.patch.object(document_import.profile_store, "UPLOAD_DIR", uploads_dir), \
                mock.patch.object(document_import.settings_store, "PATH", settings_path), \
                mock.patch.object(document_import.ai_filters, "available", return_value=True), \
                mock.patch.object(document_import.ai_filters, "generate_json", return_value=ai_result):
            result = document_import.analyse_uploads(_uploads(), brands, [])
            paths = [Path(item["path"]) for item in result["preview"]["files"]]
            assert all(path.exists() for path in paths)
            document_import.clear_preview(delete_files=True)
            assert document_import.get_preview() is None
            assert all(not path.exists() for path in paths)


def test_settings_template_exposes_multi_file_import_and_confirmation():
    template = (
        Path(__file__).resolve().parents[1] / "templates" / "settings.html"
    ).read_text(encoding="utf-8")
    assert 'action="/settings/document-import/analyse"' in template
    assert 'name="files"' in template
    assert "multiple required" in template
    assert 'action="/settings/document-import/apply"' in template
    assert "короткие текстовые фрагменты без email и телефона" in template


def test_bulk_routes_accept_many_files_and_confirm_dynamic_targets():
    client = TestClient(app.app, base_url="http://127.0.0.1")
    preview = {
        "id": "preview-1",
        "files": [],
        "groups": [{"id": "group-1", "target": "brand:netto"}],
    }
    with mock.patch.object(app, "_document_settings_options", return_value=([], [])), \
            mock.patch.object(
                app.document_import,
                "analyse_uploads",
                return_value={"ok": True, "preview": preview},
            ) as analyse:
        response = client.post(
            "/settings/document-import/analyse",
            files=[
                ("files", ("a.pdf", b"a", "application/pdf")),
                ("files", ("b.pdf", b"b", "application/pdf")),
            ],
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert analyse.call_args.args[0][0].filename == "a.pdf"
    assert len(analyse.call_args.args[0]) == 2

    with mock.patch.object(app.document_import, "get_preview", return_value=preview), \
            mock.patch.object(
                app.document_import, "apply_preview", return_value=[{"id": "rule-1"}]
            ) as apply_plan, \
            mock.patch.object(app.document_import, "clear_preview") as clear:
        response = client.post(
            "/settings/document-import/apply",
            data={
                "preview_id": "preview-1",
                "selections": "group-1|brand:netto",
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert apply_plan.call_args.args[1] == {"group-1": "brand:netto"}
    clear.assert_called_once_with()


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ")
