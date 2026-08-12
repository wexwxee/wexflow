"""Изоляция аккаунтов: владелец (Gemini+Groq) и обычный пользователь (только Groq).

Account A — владелец: Gemini key A, Groq key A.
Account B — обычный пользователь: Groq key B, Gemini отсутствует.
"""
import base64
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_gateway
import ai_secrets
import ai_usage
import app

KEY_GEM_A = "AIza_OWNER_ONLY_KEY_AAAA"
KEY_GROQ_A = "gsk_OWNER_GROQ_KEY_BBBB"
KEY_GROQ_B = "gsk_USER_B_GROQ_KEY_CCCC"


def _fake_dpapi():
    def protect(raw: str) -> str:
        return base64.b64encode(bytes(b ^ 0x5A for b in raw.encode("utf-8"))).decode("ascii")

    def unprotect(token: str) -> str:
        return bytes(b ^ 0x5A for b in base64.b64decode(token)).decode("utf-8")
    return protect, unprotect


class _Env:
    """Изолированное хранилище + два подготовленных аккаунта."""

    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        td = Path(self.td.name)
        protect, unprotect = _fake_dpapi()
        self.stack = [
            mock.patch.object(ai_secrets, "PATH", td / "cred.json"),
            mock.patch.object(ai_secrets, "_LOCAL_ID_PATH", td / "acc.json"),
            mock.patch.object(ai_secrets, "_protect", protect),
            mock.patch.object(ai_secrets, "_unprotect", unprotect),
            mock.patch.object(ai_secrets, "env_key", return_value=""),
            mock.patch.object(ai_usage, "PATH", td / "usage.json"),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in self.stack:
            p.start()
        os.environ.pop("WEXFLOW_AI_ACCOUNT", None)
        ai_secrets._cache_drop()
        ai_secrets.set_api_key("gemini", KEY_GEM_A, "A", consent=True)
        ai_secrets.set_api_key("groq", KEY_GROQ_A, "A", consent=True)
        ai_secrets.set_api_key("groq", KEY_GROQ_B, "B", consent=True)
        return self

    def __exit__(self, *exc):
        ai_secrets._cache_drop()
        for p in reversed(self.stack):
            p.stop()
        self.td.cleanup()
        return False


def test_owner_sees_both_user_sees_only_groq():
    with _Env():
        assert [n for n, _ in ai_gateway._usable_providers("A")] == ["gemini", "groq"]
        assert [n for n, _ in ai_gateway._usable_providers("B")] == ["groq"]
        assert ai_gateway.active_provider("A") == "gemini"
        assert ai_gateway.active_provider("B") == "groq"


def test_user_b_cannot_get_or_use_owner_gemini_even_as_fallback():
    with _Env():
        assert ai_secrets.get_api_key("gemini", "B") == ""
        assert ai_secrets.has_key("gemini", "B") is False
        # у B в маршруте вообще нет Gemini — резерв невозможен
        assert all(n != "gemini" for n, _ in ai_gateway._usable_providers("B"))


def test_user_b_does_not_see_owner_mask_or_usage():
    with _Env():
        with mock.patch.object(ai_secrets, "current_account_id", return_value="B"):
            payload = ai_gateway.usage_payload()
        assert payload["providers"]["gemini"]["connected"] is False
        assert payload["providers"]["gemini"]["mask"] == ""
        assert payload["providers"]["groq"]["mask"].endswith("CCCC"[-4:])
        assert KEY_GEM_A not in json.dumps(payload)
        assert KEY_GROQ_A not in json.dumps(payload)


def test_usage_stats_do_not_mix_between_accounts():
    with _Env():
        fp_a = ai_secrets.fingerprint("groq", "A")
        fp_b = ai_secrets.fingerprint("groq", "B")
        assert fp_a != fp_b
        ai_usage.record_provider("groq", "m", 200, account_id="A", fingerprint=fp_a)
        ai_usage.record_provider("groq", "m", 200, account_id="A", fingerprint=fp_a)
        ai_usage.record_provider("groq", "m", 200, account_id="B", fingerprint=fp_b)

        assert ai_usage.provider_status("groq", "A", fp_a)["requests"]["used"] == 2
        assert ai_usage.provider_status("groq", "B", fp_b)["requests"]["used"] == 1
        # события A не видны из контекста B
        assert ai_usage.provider_status("groq", "B", fp_a)["requests"]["used"] == 0


def test_api_usage_ignores_supplied_foreign_account_id():
    """/api/ai/usage отдаёт данные ТОЛЬКО текущего аккаунта; чужой id из браузера игнорируется."""
    with _Env():
        with mock.patch.object(ai_secrets, "current_account_id", return_value="B"):
            payload_b = app._ai_usage_payload()
        # endpoint не принимает account_id как параметр вовсе
        import inspect
        assert list(inspect.signature(app.api_ai_usage).parameters) == []
        assert payload_b["ai"]["providers"]["gemini"]["connected"] is False


def test_deleting_key_b_keeps_owner_keys():
    with _Env():
        assert ai_secrets.delete_api_key("groq", "B") is True
        assert ai_secrets.get_api_key("groq", "B") == ""
        assert ai_secrets.get_api_key("groq", "A") == KEY_GROQ_A
        assert ai_secrets.get_api_key("gemini", "A") == KEY_GEM_A


def test_replacing_key_b_creates_new_fingerprint_and_fresh_stats():
    with _Env():
        old_fp = ai_secrets.fingerprint("groq", "B")
        ai_usage.record_provider("groq", "m", 200, account_id="B", fingerprint=old_fp)
        ai_secrets.set_api_key("groq", "gsk_ROTATED_KEY_DDDD", "B")
        new_fp = ai_secrets.fingerprint("groq", "B")

        assert new_fp != old_fp
        assert ai_usage.provider_status("groq", "B", new_fp)["requests"]["used"] == 0


def test_logout_clears_in_memory_keys():
    with _Env():
        assert ai_secrets.get_api_key("groq", "A") == KEY_GROQ_A   # попадает в кэш
        with mock.patch.object(ai_secrets, "_KEY_CACHE", {}) as cache:
            pass
        ai_secrets.on_account_switch()
        assert ai_secrets._KEY_CACHE == {}


def test_background_task_pinned_to_its_account():
    with _Env():
        os.environ["WEXFLOW_AI_ACCOUNT"] = "A"
        try:
            assert ai_secrets.current_account_id() == "A"
            # аккаунт не менялся -> контекст валиден
            with mock.patch.object(ai_secrets, "_live_account_id", return_value="A"):
                assert ai_secrets.context_valid() is True
            # пользователь переключился на B -> задача A обязана остановиться
            with mock.patch.object(ai_secrets, "_live_account_id", return_value="B"):
                assert ai_secrets.context_valid() is False
                res = ai_gateway.generate_json("x")
                assert res.ok is False and res.error_code == "not_connected"
        finally:
            os.environ.pop("WEXFLOW_AI_ACCOUNT", None)


def test_worker_never_receives_keys_in_env_or_argv():
    """Воркер получает только account_id; ключи вычищаются из окружения потомка."""
    captured = {}

    class _Proc:
        pass

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env") or {}
        return _Proc()

    anthropic_key = "sk-ant-secret-worker-sentinel"
    with _Env(), mock.patch.dict(os.environ, {
        "GEMINI_API_KEY": KEY_GEM_A,
        "GROQ_API_KEY": KEY_GROQ_B,
        "ANTHROPIC_API_KEY": anthropic_key,
    }):
        with mock.patch.object(app, "_load_jobs_snapshot", return_value=[("job-1", None)]), \
             mock.patch.object(app.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch("builtins.open", mock.mock_open()), \
             mock.patch.object(ai_secrets, "current_account_id", return_value="A"):
            app._run_apply_worker(["job-1"], submit=False, ai_fill=True)

    env, cmd = captured["env"], captured["cmd"]
    assert all(name not in env for name in ai_secrets.PROVIDER_ENV_VARS.values())
    assert env["WEXFLOW_AI_ACCOUNT"] == "A"
    assert KEY_GEM_A not in " ".join(map(str, cmd))
    assert KEY_GROQ_B not in " ".join(map(str, cmd))
    secrets = (KEY_GEM_A, KEY_GROQ_B, anthropic_key)
    assert not any(secret in " ".join(map(str, cmd)) for secret in secrets)
    assert not any(any(secret in str(v) for secret in secrets) for v in env.values())


def test_legacy_gemini_is_not_usable_by_unconfirmed_account():
    """Regression: legacy-ключ в secrets.json НЕ даёт права аккаунту без подтверждённой привязки."""
    with tempfile.TemporaryDirectory() as td:
        secrets_path = Path(td) / "secrets.json"
        secrets_path.write_text(json.dumps({"gemini_api_key": "AIza_LEGACY_KEY"}), encoding="utf-8")
        protect, unprotect = _fake_dpapi()
        with mock.patch.object(ai_secrets, "PATH", Path(td) / "cred.json"), \
             mock.patch.object(ai_secrets, "_LOCAL_ID_PATH", Path(td) / "acc.json"), \
             mock.patch.object(ai_secrets, "_protect", protect), \
             mock.patch.object(ai_secrets, "_unprotect", unprotect), \
             mock.patch.object(ai_secrets, "env_key", return_value=""), \
             mock.patch.object(ai_secrets.config, "SECRETS_PATH", secrets_path):
            ai_secrets._cache_drop()
            # никакой автоматической миграции: ключ не принадлежит никому
            assert ai_secrets.get_api_key("gemini", "stranger") == ""
            assert ai_gateway._usable_providers("stranger") == []
            # и даже gateway не имеет права его взять
            res = ai_gateway.generate_json("x", account_id="stranger")
            assert res.error_code == "not_connected"
