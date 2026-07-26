from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
import json

import candidate_profiles
import profile_store
import app


def test_primary_profile_keeps_existing_data_and_name():
    with TemporaryDirectory() as td:
        root = Path(td)
        existing = root / "profile.json"
        existing.write_text(
            json.dumps({"first_name": "Иван", "last_name": "Мельник"}, ensure_ascii=False),
            encoding="utf-8",
        )

        state = candidate_profiles.load(root)

        assert state["active_id"] == candidate_profiles.PRIMARY_ID
        assert state["profiles"][0]["name"] == "Иван Мельник"
        assert candidate_profiles.data_dir(candidate_profiles.PRIMARY_ID, root) == root
        assert existing.exists()


def test_new_candidate_has_a_separate_empty_directory():
    with TemporaryDirectory() as td:
        root = Path(td)
        (root / "jobs.db").write_bytes(b"existing primary database")

        sister = candidate_profiles.create_profile("Сестра", root)

        sister_dir = candidate_profiles.data_dir(sister["id"], root)
        assert candidate_profiles.active_profile_id(root) == sister["id"]
        assert sister_dir.is_dir()
        assert list(sister_dir.iterdir()) == []
        assert (root / "jobs.db").read_bytes() == b"existing primary database"

        candidate_profiles.set_active(candidate_profiles.PRIMARY_ID, root)
        assert candidate_profiles.is_primary(root)


def test_duplicate_and_unknown_profiles_are_rejected():
    with TemporaryDirectory() as td:
        root = Path(td)
        candidate_profiles.create_profile("Сестра", root)
        try:
            candidate_profiles.create_profile("  сестра  ", root)
        except ValueError:
            pass
        else:
            raise AssertionError("duplicate profile name was accepted")

        try:
            candidate_profiles.set_active("missing", root)
        except ValueError:
            pass
        else:
            raise AssertionError("unknown profile id was accepted")


def test_remote_switch_request_is_one_use_and_only_accepts_existing_profile():
    with TemporaryDirectory() as td:
        root = Path(td)
        sister = candidate_profiles.create_profile("Сестра", root, activate=False)

        requested = candidate_profiles.request_remote_switch(sister["id"], root)
        assert requested["id"] == sister["id"]
        assert candidate_profiles.take_remote_switch(root)["id"] == sister["id"]
        assert candidate_profiles.take_remote_switch(root) is None

        try:
            candidate_profiles.request_remote_switch("missing", root)
        except ValueError:
            pass
        else:
            raise AssertionError("remote switch accepted an unknown profile")


def test_telegram_items_are_partitioned_by_candidate_profile():
    with TemporaryDirectory() as td:
        root = Path(td)
        sister = candidate_profiles.create_profile("Сестра", root, activate=False)
        items = [
            {"id": "legacy", "action": "scan"},
            {"id": "sister", "action": "scan", "profileId": sister["id"]},
            {"id": "forged", "action": "scan", "profileId": "person_missing"},
        ]
        with mock.patch.object(candidate_profiles, "storage_root", return_value=root):
            current, other, invalid = app._partition_tg_items(items, candidate_profiles.PRIMARY_ID)

        assert [item["id"] for item in current] == ["legacy"]
        assert [item["id"] for item in other] == ["sister"]
        assert [item["id"] for item in invalid] == ["forged"]


def test_managed_upload_is_deleted_but_external_source_is_not():
    with TemporaryDirectory() as td:
        root = Path(td)
        uploads = root / "uploads"
        uploads.mkdir()
        managed = uploads / "cv.pdf"
        external = root / "original.pdf"
        managed.write_bytes(b"copy")
        external.write_bytes(b"original")

        with mock.patch.object(profile_store, "UPLOAD_DIR", uploads):
            assert profile_store.remove_managed_document(str(managed))
            assert not managed.exists()
            assert not profile_store.remove_managed_document(str(external))
            assert external.exists()


def test_remove_global_document_clears_only_selected_profile_field():
    saved = {}
    profile = {
        "first_name": "Иван",
        "cv_path": "C:/private/uploads/cv.pdf",
        "cover_letter_path": "C:/private/uploads/cover.pdf",
    }
    with mock.patch.object(app.profile_store, "load_profile", return_value=dict(profile)), \
            mock.patch.object(app.profile_store, "save_profile", side_effect=lambda value: saved.update(value)), \
            mock.patch.object(app.profile_store, "remove_managed_document", return_value=True) as remove:
        response = app.settings_documents_save(
            cv_path="",
            cover_letter_path="",
            cv_file=None,
            cover_letter_file=None,
            remove_document="cv",
        )

    assert response.status_code == 303
    assert saved["cv_path"] == ""
    assert saved["cover_letter_path"] == profile["cover_letter_path"]
    remove.assert_called_once_with(profile["cv_path"])


def test_profile_controls_and_remove_buttons_are_present():
    sidebar = Path("templates/_ui.html").read_text(encoding="utf-8")
    settings = Path("templates/settings.html").read_text(encoding="utf-8")
    assert "data-candidate-profile-open" in sidebar
    assert "Новый профиль человека" in sidebar
    assert "Данные, документы и подачи начнутся с нуля" in sidebar
    assert "Создать профиль" in sidebar
    assert "candidate-profile-action" in sidebar
    assert "data-candidate-profile-invite" in sidebar
    assert "Telegram каждого человека связан только с его профилем" in sidebar
    assert 'name="remove_document" value="cv"' in settings
    assert 'name="remove_document" value="cover"' in settings
