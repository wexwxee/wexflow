"""Per-brand and per-store document selection."""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import document_rules


def _job(**changes):
    data = {
        "brand": "netto",
        "street": "Herlev Hovedgade 25",
        "zip": "2730",
        "city": "Herlev",
    }
    data.update(changes)
    return SimpleNamespace(**data)


def test_store_then_brand_then_global_priority_is_per_document():
    with tempfile.TemporaryDirectory() as td:
        settings_path = Path(td) / "settings.json"
        with mock.patch.object(document_rules.settings_store, "PATH", settings_path):
            brand_rule = document_rules.save_rule(
                scope="brand",
                brand="Netto",
                brand_label="Netto",
                cv_path="netto_cv.pdf",
                cover_letter_path="",
            )
            document_rules.save_rule(
                scope="store",
                brand="netto",
                brand_label="Netto",
                selected_store_key=document_rules.store_key(_job()),
                store_label="Netto · Herlev",
                cv_path="",
                cover_letter_path="herlev_letter.docx",
            )

            resolved = document_rules.resolve_profile(
                {"cv_path": "global_cv.pdf", "cover_letter_path": "global_letter.pdf"},
                _job(),
            )
            other_store = document_rules.resolve_profile(
                {"cv_path": "global_cv.pdf", "cover_letter_path": "global_letter.pdf"},
                _job(street="Anden Vej 1"),
            )

    assert resolved["cv_path"] == "netto_cv.pdf"
    assert resolved["cover_letter_path"] == "herlev_letter.docx"
    assert resolved["_document_selection"]["cv"]["rule_id"] == brand_rule["id"]
    assert resolved["_document_selection"]["cover"]["level"] == "store"
    assert other_store["cv_path"] == "netto_cv.pdf"
    assert other_store["cover_letter_path"] == "global_letter.pdf"


def test_store_key_is_stable_for_case_and_spacing():
    first = _job(street=" Herlev   Hovedgade 25 ", city="HERLEV")
    second = _job(street="herlev hovedgade 25", city="herlev")
    assert document_rules.store_key(first) == document_rules.store_key(second)


def test_rule_update_and_delete_keep_one_record():
    with tempfile.TemporaryDirectory() as td:
        settings_path = Path(td) / "settings.json"
        with mock.patch.object(document_rules.settings_store, "PATH", settings_path):
            saved = document_rules.save_rule(
                scope="brand",
                brand="foetex",
                brand_label="Føtex",
                cv_path="old.pdf",
            )
            updated = document_rules.save_rule(
                rule_id=saved["id"],
                name="Føtex новый",
                scope="brand",
                brand="foetex",
                brand_label="Føtex",
                cv_path="new.pdf",
            )
            assert len(document_rules.get_rules()) == 1
            assert updated["cv_path"] == "new.pdf"
            assert document_rules.delete_rule(saved["id"]) is True
            assert document_rules.get_rules() == []


def test_same_target_is_merged_instead_of_creating_ambiguous_duplicate():
    with tempfile.TemporaryDirectory() as td:
        settings_path = Path(td) / "settings.json"
        with mock.patch.object(document_rules.settings_store, "PATH", settings_path):
            first = document_rules.save_rule(
                scope="brand",
                brand="netto",
                brand_label="Netto",
                cv_path="netto.pdf",
            )
            second = document_rules.save_rule(
                scope="brand",
                brand="netto",
                brand_label="Netto",
                cover_letter_path="netto_letter.pdf",
            )
            rules = document_rules.get_rules()

    assert second["id"] == first["id"]
    assert len(rules) == 1
    assert rules[0]["cv_path"] == "netto.pdf"
    assert rules[0]["cover_letter_path"] == "netto_letter.pdf"


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
    print(f"\nВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ")
