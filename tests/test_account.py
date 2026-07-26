"""Локальный выход должен переживать фоновые опросы и перезапуск."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import account


def test_sign_out_pauses_automatic_cloud_login():
    original = account.ACCOUNT_PATH
    original_sync = account._sync_subscription
    try:
        account.ACCOUNT_PATH = Path(tempfile.mkdtemp()) / "account.json"
        account._sync_subscription = lambda _plan: None
        account.apply_session({"tgId": "42", "name": "Ivan", "plan": "pro"})
        assert account.is_signed_in() and not account.cloud_sync_paused()
        account.sign_out()
        assert not account.is_signed_in()
        assert account.cloud_sync_paused()
        saved = json.loads(account.ACCOUNT_PATH.read_text(encoding="utf-8"))
        assert saved["cloud_sync_paused"] is True
        account.apply_session({"tgId": "42", "name": "Ivan", "plan": "free"})
        assert account.is_signed_in() and not account.cloud_sync_paused()
    finally:
        account.ACCOUNT_PATH = original
        account._sync_subscription = original_sync


def test_background_cloud_session_cannot_undo_logout():
    original = account.ACCOUNT_PATH
    original_sync = account._sync_subscription
    try:
        account.ACCOUNT_PATH = Path(tempfile.mkdtemp()) / "account.json"
        account._sync_subscription = lambda _plan: None
        account.sign_out()
        assert account.apply_cloud_session({"tgId": "42", "plan": "pro"}) is False
        assert account.load()["signed_in"] is False
        assert account.load()["cloud_sync_paused"] is True

        # Осознанный новый вход, в отличие от фонового poll, снимает паузу.
        account.apply_session({"tgId": "42", "plan": "pro"})
        assert account.load()["signed_in"] is True
        assert account.load()["cloud_sync_paused"] is False
    finally:
        account.ACCOUNT_PATH = original
        account._sync_subscription = original_sync


def test_family_identity_never_falls_back_to_shared_owner_account():
    profile = {"first_name": "", "last_name": "", "email": ""}
    owner = {
        "signed_in": True,
        "tg_name": "Owner",
        "username": "owner_tg",
        "plan": "pro",
    }
    sister = {
        "linked": True,
        "name": "Nastya Telegram",
        "username": "nastya_tg",
        "tgId": "777",
        "plan": "free",
    }
    original = account.load
    try:
        account.load = lambda: dict(owner)
        family = account.status(profile, sister)
        unlinked = account.status(profile, {"linked": False})
    finally:
        account.load = original

    assert family["signed_in"] is True
    assert family["tg_name"] == "Nastya Telegram"
    assert family["username"] == "nastya_tg"
    assert family["plan"] == "free"
    assert unlinked["signed_in"] is False
    assert unlinked["username"] == ""


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"OK   {test.__name__}")
