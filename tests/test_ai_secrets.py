"""Безопасное хранилище ИИ-ключей: DPAPI (mock), изоляция аккаунтов, маскирование."""
import base64
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_secrets


def _fake_dpapi():
    """Обратимая псевдо-DPAPI для тестов: шифртекст != открытый текст."""
    def protect(raw: str) -> str:
        return base64.b64encode(bytes(b ^ 0x5A for b in raw.encode("utf-8"))).decode("ascii")

    def unprotect(token: str) -> str:
        return bytes(b ^ 0x5A for b in base64.b64decode(token)).decode("utf-8")
    return protect, unprotect


def _env(td):
    protect, unprotect = _fake_dpapi()
    return (
        mock.patch.object(ai_secrets, "PATH", Path(td) / "ai_credentials.json"),
        mock.patch.object(ai_secrets, "_LOCAL_ID_PATH", Path(td) / "ai_account.json"),
        mock.patch.object(ai_secrets, "_protect", protect),
        mock.patch.object(ai_secrets, "_unprotect", unprotect),
        mock.patch.object(ai_secrets, "env_key", return_value=""),
        mock.patch.object(ai_secrets, "current_account_id", return_value="acctA"),
    )


def test_round_trip_and_no_plaintext_in_file():
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.multiple(ai_secrets,
                                 PATH=Path(td) / "c.json",
                                 _LOCAL_ID_PATH=Path(td) / "a.json"), \
             mock.patch.object(ai_secrets, "env_key", return_value=""):
            protect, unprotect = _fake_dpapi()
            with mock.patch.object(ai_secrets, "_protect", protect), \
                 mock.patch.object(ai_secrets, "_unprotect", unprotect):
                key = "gsk_SUPERSECRET_ABCD"
                ai_secrets._cache_drop()
                info = ai_secrets.set_api_key("groq", key, "acctA", consent=True)

                assert ai_secrets.get_api_key("groq", "acctA") == key
                assert info["mask"] == "••••ABCD"
                assert key not in json.dumps(info)             # маска, не ключ
                raw_file = (Path(td) / "c.json").read_text(encoding="utf-8")
                assert key not in raw_file                      # в файле только шифртекст
                assert info["fingerprint"] and len(info["fingerprint"]) == 12


def test_accounts_are_isolated():
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.multiple(ai_secrets,
                                 PATH=Path(td) / "c.json",
                                 _LOCAL_ID_PATH=Path(td) / "a.json"), \
             mock.patch.object(ai_secrets, "env_key", return_value=""), \
             mock.patch.object(ai_secrets, "current_account_id", return_value="acctA"):
            protect, unprotect = _fake_dpapi()
            with mock.patch.object(ai_secrets, "_protect", protect), \
                 mock.patch.object(ai_secrets, "_unprotect", unprotect):
                ai_secrets._cache_drop()
                ai_secrets.set_api_key("gemini", "AIza_OWNER_KEY_1", "acctA", consent=True)
                ai_secrets.set_api_key("groq", "gsk_USER_B_KEY_2", "acctB", consent=True)

                # A видит свой Gemini; B — только свой Groq
                assert ai_secrets.get_api_key("gemini", "acctA") == "AIza_OWNER_KEY_1"
                assert ai_secrets.get_api_key("groq", "acctB") == "gsk_USER_B_KEY_2"
                # B НЕ видит Gemini владельца — ни ключ, ни маску
                assert ai_secrets.get_api_key("gemini", "acctB") == ""
                assert ai_secrets.has_key("gemini", "acctB") is False
                assert ai_secrets.info("gemini", "acctB")["mask"] == ""
                # A не видит Groq пользователя B
                assert ai_secrets.get_api_key("groq", "acctA") == ""


def test_replace_creates_new_fingerprint_and_delete_is_scoped():
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.multiple(ai_secrets,
                                 PATH=Path(td) / "c.json",
                                 _LOCAL_ID_PATH=Path(td) / "a.json"), \
             mock.patch.object(ai_secrets, "env_key", return_value=""), \
             mock.patch.object(ai_secrets, "current_account_id", return_value="acctA"):
            protect, unprotect = _fake_dpapi()
            with mock.patch.object(ai_secrets, "_protect", protect), \
                 mock.patch.object(ai_secrets, "_unprotect", unprotect):
                ai_secrets._cache_drop()
                ai_secrets.set_api_key("gemini", "AIza_OWNER", "acctA", consent=True)
                ai_secrets.set_api_key("groq", "gsk_B", "acctB", consent=True)
                fp1 = ai_secrets.fingerprint("groq", "acctB")
                ai_secrets.set_api_key("groq", "gsk_B_ROTATED", "acctB")
                fp2 = ai_secrets.fingerprint("groq", "acctB")
                assert fp1 != fp2                               # новый ключ -> новый отпечаток

                # Удаление ключа B не трогает ключи A
                assert ai_secrets.delete_api_key("groq", "acctB") is True
                assert ai_secrets.get_api_key("groq", "acctB") == ""
                assert ai_secrets.get_api_key("gemini", "acctA") == "AIza_OWNER"


def test_legacy_migration_binds_to_confirmed_account_only():
    with tempfile.TemporaryDirectory() as td:
        secrets_path = Path(td) / "secrets.json"
        secrets_path.write_text(json.dumps({"gemini_api_key": "AIza_LEGACY_9999"}), encoding="utf-8")
        with mock.patch.multiple(ai_secrets,
                                 PATH=Path(td) / "c.json",
                                 _LOCAL_ID_PATH=Path(td) / "a.json"), \
             mock.patch.object(ai_secrets, "env_key", return_value=""), \
             mock.patch.object(ai_secrets.config, "SECRETS_PATH", secrets_path):
            protect, unprotect = _fake_dpapi()
            with mock.patch.object(ai_secrets, "_protect", protect), \
                 mock.patch.object(ai_secrets, "_unprotect", unprotect):
                ai_secrets._cache_drop()
                # Без confirm ничего не мигрирует
                assert ai_secrets.migrate_legacy_gemini("owner", confirm=False) is False
                # С confirm — привязка только к аккаунту owner
                assert ai_secrets.migrate_legacy_gemini("owner", confirm=True) is True
                assert ai_secrets.get_api_key("gemini", "owner") == "AIza_LEGACY_9999"
                # Другой аккаунт по-прежнему не видит legacy-ключ
                assert ai_secrets.get_api_key("gemini", "stranger") == ""


def test_account_switch_clears_in_memory_cache():
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.multiple(ai_secrets,
                                 PATH=Path(td) / "c.json",
                                 _LOCAL_ID_PATH=Path(td) / "a.json"), \
             mock.patch.object(ai_secrets, "env_key", return_value=""), \
             mock.patch.object(ai_secrets, "current_account_id", return_value="acctA"):
            protect, unprotect = _fake_dpapi()
            with mock.patch.object(ai_secrets, "_protect", protect), \
                 mock.patch.object(ai_secrets, "_unprotect", unprotect):
                ai_secrets._cache_drop()
                ai_secrets.set_api_key("groq", "gsk_LIVE", "acctA", consent=True)
                assert ai_secrets.get_api_key("groq", "acctA") == "gsk_LIVE"  # кэшируется
                ai_secrets.delete_api_key("groq", "acctA")
                ai_secrets.on_account_switch()                  # смена аккаунта чистит кэш
                assert ai_secrets.get_api_key("groq", "acctA") == ""
