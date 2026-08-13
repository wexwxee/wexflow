"""Обязательный вход: у подписки и блокировки должен быть владелец.

Главная опасность такой двери — запереть самого пользователя. Поэтому проверка
идёт в обе стороны: без входа приложение закрыто, но упавшее облако доступ НЕ
отнимает, а страница входа и статика остаются открытыми всегда.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import account as account_mod
import app as salling_app


@pytest.fixture()
def client():
    return TestClient(salling_app.app, base_url="http://127.0.0.1",
                      follow_redirects=False)


def _state(name: str, reason: str = ""):
    return mock.patch.object(
        account_mod, "access_state", return_value={"state": name, "reason": reason}
    )


def test_without_login_pages_lead_to_the_account_screen(client):
    with _state("login_required"):
        for path in ("/", "/audit", "/settings", "/autopilot", "/job/whatever"):
            response = client.get(path)
            assert response.status_code == 303, path
            assert response.headers["location"] == "/account?gate=login", path


def test_the_way_in_is_never_blocked(client):
    """Иначе человек упирается в дверь, за которой лежит ключ от неё."""
    with _state("login_required"):
        for path in ("/account", "/account?gate=login", "/static/theme.css",
                     "/api/version", "/help"):
            assert client.get(path).status_code in (200, 304, 307), path


def test_api_answers_with_a_code_a_program_can_read(client):
    with _state("login_required"):
        response = client.get("/api/sync-status")
    assert response.status_code == 401
    assert response.json()["code"] == "login_required"

    with _state("banned", "правила"):
        response = client.get("/api/sync-status")
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "banned" and "правила" in body["error"]


def test_a_banned_account_is_told_why(client):
    with _state("banned", "массовые отклики"):
        response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/account?gate=banned"


def test_a_dead_cloud_does_not_lock_the_owner_out():
    """Vercel упал или кончился Upstash — приложение обязано работать."""
    saved = {
        "signed_in": True, "tg_id": "1", "plan": "free",
        "banned": False, "ban_reason": None, "cloud_sync_paused": False,
        "tg_name": None, "username": None, "email": None,
    }
    with mock.patch.object(account_mod, "load", return_value=saved):
        assert account_mod.access_state()["state"] == "ok"


def test_ban_from_the_cloud_is_stored_and_lifted():
    saved = {}
    with mock.patch.object(account_mod, "load", side_effect=lambda: dict(saved)), \
            mock.patch.object(account_mod, "save", side_effect=saved.update), \
            mock.patch.object(account_mod, "_sync_subscription"), \
            mock.patch.object(account_mod, "_drop_ai_keys"):
        account_mod.apply_session({"tgId": "7", "plan": "pro", "banned": True,
                                   "banReason": "спам"})
        assert saved["banned"] is True and saved["ban_reason"] == "спам"
        account_mod.apply_session({"tgId": "7", "plan": "pro"})
        assert saved["banned"] is False and saved["ban_reason"] is None


def test_the_login_screen_explains_itself():
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent
            / "templates" / "account.html").read_text(encoding="utf-8")
    assert "gate == 'login'" in html and "gate == 'banned'" in html
    assert "чтобы у подписки был владелец" in html
    assert "wexwxeee" in html          # куда писать, если блокировка ошибочна
    assert "данные на этом компьютере не тронуты" in html
