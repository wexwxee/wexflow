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


if __name__ == "__main__":
    test_sign_out_pauses_automatic_cloud_login()
    print("OK   test_sign_out_pauses_automatic_cloud_login")
