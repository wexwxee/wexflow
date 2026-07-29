"""Тесты токена устройства (шаг 5, Блок 2).

Профиль и очереди в облаке больше не отдаются любому, кто знает device id:
приложение локально придумывает секрет, один раз регистрирует его в облаке
и подписывает каждый запрос заголовком x-device-token.

Проверяем клиентскую часть (cloud_auth) без сети: urllib подменяется.

Запуск:  python tests/test_device_token.py   (или pytest)
"""
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cloud_auth
import config


class _TempDevice:
    """Временный device.json + сброс кэшей модуля."""
    def __enter__(self):
        self._orig_path = cloud_auth.DEVICE_PATH
        self._orig_cache = cloud_auth._device_cache
        self._orig_reg = cloud_auth._registered
        self.dir = Path(tempfile.mkdtemp())
        cloud_auth.DEVICE_PATH = self.dir / "device.json"
        cloud_auth._device_cache = None
        cloud_auth._registered = False
        return self

    def __exit__(self, *exc):
        cloud_auth.DEVICE_PATH = self._orig_path
        cloud_auth._device_cache = self._orig_cache
        cloud_auth._registered = self._orig_reg


class _FakeCloud:
    """Подмена urllib.request.urlopen: пишет все запросы, отвечает ok."""
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        headers = dict(getattr(req, "headers", {}) or {})
        body = getattr(req, "data", None)
        self.requests.append({
            "url": url,
            "headers": {k.lower(): v for k, v in headers.items()},
            "body": json.loads(body.decode("utf-8")) if body else None,
        })
        payload = ({"ok": True, "ack": True, "decisions": [], "commands": []}
                   if "kind=poll2" in url else {"ok": True, "loggedIn": False})
        resp = io.BytesIO(json.dumps(payload).encode("utf-8"))
        resp.__enter__ = lambda *a: resp
        resp.__exit__ = lambda *a: False
        return resp


class _Patched:
    def __enter__(self):
        self._orig = cloud_auth.urllib.request.urlopen
        self.cloud = _FakeCloud()
        cloud_auth.urllib.request.urlopen = self.cloud
        return self.cloud

    def __exit__(self, *exc):
        cloud_auth.urllib.request.urlopen = self._orig


def test_device_record_created_with_secret():
    with _TempDevice():
        rec = cloud_auth._device_record()
        assert rec["id"] and len(rec["secret"]) == 64 and rec["persisted"]
        saved = json.loads(cloud_auth.DEVICE_PATH.read_text(encoding="utf-8"))
        assert saved == {"id": rec["id"], "secret": rec["secret"]}


def test_old_device_json_upgraded_keeps_id():
    with _TempDevice():
        cloud_auth.DEVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
        cloud_auth.DEVICE_PATH.write_text(json.dumps({"id": "olddevice42"}), encoding="utf-8")
        rec = cloud_auth._device_record()
        assert rec["id"] == "olddevice42", "id старой установки потерян"
        assert rec["secret"], "секрет не дописан старой установке"


def test_requests_carry_token_and_register_once():
    with _TempDevice(), _Patched() as cloud:
        cloud_auth.fetch_session()
        cloud_auth.fetch_decisions()
        reg = [r for r in cloud.requests
               if r["body"] and "secret" in r["body"]]
        assert len(reg) == 1, f"регистраций {len(reg)}, ожидали 1"
        assert reg[0]["body"]["secret"] == cloud_auth.device_secret()
        others = [r for r in cloud.requests if r not in reg]
        assert others, "рабочих запросов не было"
        for r in others:
            assert r["headers"].get("x-device-token") == cloud_auth.device_secret(), \
                f"запрос без токена: {r['url']}"


def test_combined_poll_uses_one_authenticated_request():
    with _TempDevice(), _Patched() as cloud:
        result = cloud_auth.fetch_poll(tg_id="42", sync_binding=True)
        # active — признак «панель открыта в телефоне» (ПК слушает часто)
        assert result == {"decisions": [], "commands": [], "ack": True, "active": False}
        polls = [r for r in cloud.requests if "kind=poll2" in r["url"]]
        assert len(polls) == 1
        assert "tgId=42" in polls[0]["url"]
        assert "bind=1" in polls[0]["url"]
        assert polls[0]["headers"].get("x-device-token") == cloud_auth.device_secret()


def test_idle_poll_does_not_spend_requests_on_binding_sync():
    with _TempDevice(), _Patched() as cloud:
        cloud_auth.fetch_poll(tg_id="42")
        poll = next(r for r in cloud.requests if "kind=poll2" in r["url"])
        assert "tgId=" not in poll["url"]
        assert "bind=1" not in poll["url"]


def test_cloud_quota_error_is_human_readable():
    result = cloud_auth._friendly_cloud_result({
        "ok": False,
        "code": "store_quota",
        "error": "ERR max requests limit exceeded",
    }, http_status=503)

    assert result["code"] == "store_quota"
    assert "исчерпало лимит" in result["error"]
    assert "500" not in result["error"]


def test_combined_poll_preserves_cloud_quota_reason():
    response = io.BytesIO(json.dumps({
        "ok": False,
        "code": "store_quota",
        "error": "ERR max requests limit exceeded",
    }).encode("utf-8"))
    response.__enter__ = lambda *a: response
    response.__exit__ = lambda *a: False

    with mock.patch.object(cloud_auth, "_open", return_value=response):
        assert cloud_auth.fetch_poll() is None

    error = cloud_auth.last_poll_error()
    assert error["code"] == "store_quota"
    assert "исчерпало лимит" in error["error"]


def test_combined_poll_falls_back_for_old_cloud():
    class _OldCloud(_FakeCloud):
        def __call__(self, req, timeout=None):
            response = super().__call__(req, timeout)
            if "kind=poll2" in self.requests[-1]["url"]:
                response = io.BytesIO(json.dumps({"ok": True, "decisions": []}).encode("utf-8"))
                response.__enter__ = lambda *a: response
                response.__exit__ = lambda *a: False
            return response

    with _TempDevice():
        original = cloud_auth.urllib.request.urlopen
        old_cloud = _OldCloud()
        cloud_auth.urllib.request.urlopen = old_cloud
        try:
            result = cloud_auth.fetch_poll(tg_id="42")
        finally:
            cloud_auth.urllib.request.urlopen = original
            assert result == {"decisions": [], "commands": [], "ack": False, "active": False}
        assert len([r for r in old_cloud.requests if "/api/decisions" in r["url"]]) == 2


def test_poll_ack_sends_delivery_ids():
    with _TempDevice(), _Patched() as cloud:
        assert cloud_auth.acknowledge_poll(
            [{"_deliveryId": "decision:j1:submit:1"}],
            [{"_deliveryId": "c1"}],
        )
        ack = next(r for r in cloud.requests if r["body"] and r["body"].get("kind") == "poll_ack")
        assert ack["body"]["decisionIds"] == ["decision:j1:submit:1"]
        assert ack["body"]["commandIds"] == ["c1"]


def test_no_registration_when_secret_not_persisted():
    with _TempDevice(), _Patched() as cloud:
        cloud_auth._device_cache = {"id": "x1", "secret": "s" * 64, "persisted": False}
        cloud_auth.fetch_session()
        reg = [r for r in cloud.requests if r["body"] and "secret" in r["body"]]
        assert not reg, "нельзя регистрировать несохранённый секрет (потеряется при рестарте)"


def test_delete_cloud_data_rotates_device_identity():
    with _TempDevice(), _Patched() as cloud:
        old_id = cloud_auth.device_id()
        old_secret = cloud_auth.device_secret()
        result = cloud_auth.delete_cloud_data()
        assert result["ok"] and result["identityRotated"]
        assert cloud_auth.device_id() != old_id
        assert cloud_auth.device_secret() != old_secret
        saved = json.loads(cloud_auth.DEVICE_PATH.read_text(encoding="utf-8"))
        assert saved["id"] == cloud_auth.device_id()
        delete = next(r for r in cloud.requests if r["body"] and r["body"].get("action") == "delete_data")
        assert delete["body"]["device"] == old_id
        assert delete["headers"].get("x-device-token") == old_secret


def test_simple_telegram_message_returns_full_cloud_result():
    result = {"ok": False, "needsBotStart": True, "botUrl": "https://t.me/wexflowbot"}
    with (
        mock.patch.object(cloud_auth, "device_id", return_value="device-42"),
        mock.patch.object(cloud_auth, "_post_json", return_value=result) as post,
    ):
        returned = cloud_auth.send_test_message("hello", timeout=7)

    assert returned is result
    post.assert_called_once_with(
        "/api/offer",
        {
            "deviceId": "device-42", "profileId": "primary",
            "digest": True, "text": "hello",
        },
        7,
    )


def test_digest_keeps_boolean_compatibility():
    with mock.patch.object(cloud_auth, "send_test_message", return_value={"ok": True}) as send:
        assert cloud_auth.send_digest("daily") is True
    send.assert_called_once_with("daily", 10)


def test_unlink_device_keeps_identity_and_calls_authenticated_session_route():
    result = {"ok": True, "unlinked": True}
    with (
        mock.patch.object(cloud_auth, "device_id", return_value="device-42"),
        mock.patch.object(cloud_auth, "_post_json", return_value=result) as post,
    ):
        returned = cloud_auth.unlink_device(timeout=9)

    assert returned is result
    post.assert_called_once_with(
        "/api/session",
        {"action": "unlink_device", "device": "device-42"},
        9,
    )


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"OK   {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print("\n" + (f"ВСЕ {len(tests)} ТЕСТОВ ПРОШЛИ" if not failures else f"{failures} ТЕСТ(ОВ) УПАЛО"))
    sys.exit(1 if failures else 0)
