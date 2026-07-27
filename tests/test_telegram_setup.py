"""Telegram connection wizard, login gate and detach behavior."""
import asyncio
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.requests import Request

import app


def _request(payload: dict | None = None) -> Request:
    body = json.dumps(payload or {}).encode("utf-8")
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/telegram/test",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 50000),
        },
        receive,
    )


def _json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_every_mutating_telegram_action_requires_login():
    with (
        mock.patch.object(app.account_mod, "is_signed_in", return_value=False),
        mock.patch.object(app.autopilot, "tg_pending_clear_all") as clear,
        mock.patch.object(app.autopilot, "set_tg_digest") as digest,
        mock.patch.object(app.autopilot, "set_mode") as set_mode,
        mock.patch.object(app.cloud_auth, "send_test_message") as setup_send,
        mock.patch.object(app.cloud_auth, "offer") as offer,
    ):
        responses = [
            app.api_telegram_clear_pending(),
            asyncio.run(app.api_telegram_digest(_request({"enabled": True}))),
            asyncio.run(app.api_autopilot_mode(_request({"mode": "telegram"}))),
            asyncio.run(app.telegram_approval(_request({"on": True}))),
            app.telegram_test(),
            app.telegram_setup_test(),
            asyncio.run(app.telegram_send_current(_request())),
        ]

    assert all(response.status_code == 401 for response in responses)
    assert all(_json(response)["code"] == "login_required" for response in responses)
    assert all(_json(response)["setupUrl"] == "/account#telegram-setup" for response in responses)
    clear.assert_not_called()
    digest.assert_not_called()
    set_mode.assert_not_called()
    setup_send.assert_not_called()
    offer.assert_not_called()


def test_setup_test_explains_when_bot_needs_start():
    cloud_result = {
        "ok": False,
        "code": "bot_not_started",
        "error": "Open the bot first",
        "needsBotStart": True,
        "botUrl": "https://t.me/wexflowbot?start=wexflow",
    }
    with (
        mock.patch.object(app.account_mod, "is_signed_in", return_value=True),
        mock.patch.object(app.cloud_auth, "send_test_message", return_value=cloud_result) as send,
    ):
        response = app.telegram_setup_test()

    data = _json(response)
    assert response.status_code == 200
    assert data == cloud_result
    assert "WexFlow" in send.call_args.args[0]


def test_setup_test_preserves_cloud_quota_reason():
    cloud_result = {
        "ok": False,
        "code": "store_quota",
        "error": "Облачное хранилище Telegram исчерпало лимит.",
    }
    with (
        mock.patch.object(app.account_mod, "is_signed_in", return_value=True),
        mock.patch.object(app.cloud_auth, "send_test_message", return_value=cloud_result),
    ):
        data = _json(app.telegram_setup_test())

    assert data["code"] == "store_quota"
    assert "исчерпало лимит" in data["error"]


def test_detach_stops_telegram_mode_before_local_signout():
    calls = []
    with (
        mock.patch.object(app.account_mod, "is_signed_in", return_value=True),
        mock.patch.object(app.cloud_auth, "unlink_device",
                          side_effect=lambda: calls.append(("cloud_unlink", None)) or {"ok": True}),
        mock.patch.object(app.autopilot, "get_mode", return_value="telegram"),
        mock.patch.object(app.autopilot, "set_mode", side_effect=lambda mode: calls.append(("mode", mode))),
        mock.patch.object(app, "_reschedule_autopilot_scan", side_effect=lambda: calls.append(("reschedule", None))),
        mock.patch.object(app.account_mod, "sign_out", side_effect=lambda: calls.append(("sign_out", None))),
    ):
        response = app.account_logout()

    assert response.status_code == 303
    assert response.headers["location"] == "/account?unlinked=1"
    assert calls == [
        ("mode", "off"), ("reschedule", None),
        ("sign_out", None), ("cloud_unlink", None),
    ]


def test_detach_is_locally_safe_when_cloud_is_offline():
    with (
        mock.patch.object(app.account_mod, "is_signed_in", return_value=True),
        mock.patch.object(app.cloud_auth, "unlink_device", return_value={"ok": False}),
        mock.patch.object(app.autopilot, "get_mode", return_value="off"),
        mock.patch.object(app.account_mod, "sign_out") as sign_out,
    ):
        response = app.account_logout()

    sign_out.assert_called_once()
    assert response.headers["location"] == "/account?unlink_warning=cloud"


def _account_html(signed_in: bool, *, relink: bool = False) -> str:
    account = {
        "signed_in": signed_in,
        "display_name": "Ivan",
        "initial": "I",
        "username": "ivan" if signed_in else "",
    }
    return app.templates.env.get_template("account.html").render({
        "account": account,
        "account_tg_id": "42" if signed_in else "",
        "cloud_login_url": "https://example.test/login",
        "telegram_ready": bool(signed_in and not relink),
        "telegram_relink": bool(signed_in and relink),
        "profile": {},
        "file_info": {},
        "saved": "",
        "missing_fields": [],
        "deleted": "",
        "delete_error": "",
        "unlinked": "",
        "unlink_warning": "",
        "city_options": [],
        "country_options": [],
        "subscription": {},
        "ai_fill_on": False,
        "ai_fill_motivation_on": False,
        "ai_fill_available": False,
    })


def test_signed_out_account_has_one_prominent_three_step_entry():
    html = _account_html(False)
    wizard = html.split('id="telegram-setup"', 1)[1].split("</section>", 1)[0]

    assert 'data-signed-in="0"' in html
    assert 'id="tgLoginBtn"' in wizard
    assert 'id="linkIdBtn"' in wizard
    assert wizard.index("Войди и привяжи") < wizard.index("Открой @wexflowbot") < wizard.index("Проверь сообщение")
    assert 'id="tgSetupTest"' not in wizard
    assert 'href="/settings/telegram"' not in wizard


def test_signed_in_account_exposes_test_settings_and_honest_detach():
    html = _account_html(True)
    wizard = html.split('id="telegram-setup"', 1)[1].split("</section>", 1)[0]

    assert 'data-signed-in="1"' in html
    assert 'id="tgLoginBtn"' not in wizard
    assert 'id="tgSetupTest"' in wizard
    assert 'href="/settings/telegram"' in wizard
    assert "Отвязать от этого ПК" in wizard


def test_stale_local_telegram_session_offers_relink_instead_of_claiming_connected():
    html = _account_html(True, relink=True)
    wizard = html.split('id="telegram-setup"', 1)[1].split("</section>", 1)[0]

    assert 'data-signed-in="0"' in html
    assert "Восстанови связь с Telegram" in wizard
    assert "Нужна привязка" in wizard
    assert 'id="linkIdBtn"' in wizard
    assert "Создать новый код" in wizard
    assert 'id="tgSetupTest"' not in wizard


def test_setup_test_turns_missing_device_into_one_click_relink():
    cloud_result = {"ok": False, "error": "device not linked"}
    with (
        mock.patch.object(app.account_mod, "is_signed_in", return_value=True),
        mock.patch.object(app.cloud_auth, "send_test_message", return_value=cloud_result),
        mock.patch.object(
            app.cloud_auth,
            "link_new",
            return_value={"ok": True, "code": "ABC234", "botUsername": "wexflowbot"},
        ),
    ):
        data = _json(app.telegram_setup_test())

    assert data["code"] == "device_not_linked"
    assert data["needsRelink"] is True
    assert data["linkCode"] == "ABC234"
    assert data["botUrl"].endswith("?start=ABC234")


def test_telegram_settings_locked_state_points_back_to_wizard():
    source = app.templates.env.get_template("settings.html").render({
        "settings_section": "telegram",
        "profile": {},
        "rule": {},
        "subscription": {},
    })

    assert "/account#telegram-setup" in source
    assert "Подключить Telegram пошагово" in source
    locked_branch = source.split("if(!st.signed_in)", 1)[1].split("return;", 1)[0]
    assert "https://t.me/wexflowbot" not in locked_branch

    template_source = app.templates.env.loader.get_source(
        app.templates.env, "settings.html",
    )[0]
    assert "нужен вход" in template_source
    assert 'data-setup="/account#telegram-setup"' in template_source


def test_phone_test_command_sends_same_demo_card_as_the_app_button():
    """Кнопка «Отправить проверочное» в Telegram-панели = кнопка в приложении."""
    with (
        mock.patch.object(app.account_mod, "is_signed_in", return_value=True),
        mock.patch.object(app, "_tg_card", return_value="карточка"),
        mock.patch.object(app.autopilot, "find_matches", return_value=[object()]),
        mock.patch.object(app.cloud_auth, "offer", return_value={"ok": True}) as offer,
    ):
        answer = app._handle_tg_remote_command({"action": "test"})

    assert answer == ""  # карточка сама и есть ответ — второе сообщение не шлём
    assert offer.call_args.args[1] == "__demo__"
    assert offer.call_args.kwargs.get("demo") is True


def test_phone_test_command_explains_failure_in_chat():
    with (
        mock.patch.object(app.account_mod, "is_signed_in", return_value=True),
        mock.patch.object(app, "_tg_card", return_value="карточка"),
        mock.patch.object(app.autopilot, "find_matches", return_value=[object()]),
        mock.patch.object(app.cloud_auth, "offer", return_value=None),
    ):
        answer = app._handle_tg_remote_command({"action": "test"})

    assert answer.startswith("⚠️")
    assert "нет связи с облаком" in answer
