"""Encrypted Lidl credentials and automatic portal login."""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lidl_credentials_store
import lidl_monitor


def test_password_is_encrypted_and_empty_update_keeps_it():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(
                lidl_credentials_store, "PATH", Path(tmp) / "lidl_credentials.json"
            ), \
            mock.patch.object(
                lidl_credentials_store, "_encrypt",
                side_effect=lambda value: "encrypted:" + value[::-1],
            ), \
            mock.patch.object(
                lidl_credentials_store, "_decrypt",
                side_effect=lambda value: value.removeprefix("encrypted:")[::-1],
            ):
        lidl_credentials_store.save("ivan@example.com", "Secret-123")
        raw = lidl_credentials_store.PATH.read_text(encoding="utf-8")
        assert "Secret-123" not in raw
        assert '"password"' not in raw
        assert '"password_enc"' in raw
        assert lidl_credentials_store.get() == {
            "email": "ivan@example.com",
            "password": "Secret-123",
        }

        lidl_credentials_store.save("new@example.com", "")
        assert lidl_credentials_store.get() == {
            "email": "new@example.com",
            "password": "Secret-123",
        }
        assert lidl_credentials_store.status()["has_password"] is True


def test_credentials_can_be_removed_without_touching_browser_session():
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(
                lidl_credentials_store, "PATH", Path(tmp) / "lidl_credentials.json"
            ), \
            mock.patch.object(lidl_credentials_store, "_encrypt", return_value="token"):
        lidl_credentials_store.save("ivan@example.com", "secret")
        assert lidl_credentials_store.PATH.exists()
        lidl_credentials_store.clear()
        assert not lidl_credentials_store.PATH.exists()


class _Locator:
    def __init__(self, present=True):
        self.present = present
        self.value = ""
        self.clicked = False
        self.pressed = ""

    @property
    def first(self):
        return self

    def count(self):
        return int(self.present)

    def is_visible(self):
        return self.present

    def fill(self, value):
        self.value = value

    def click(self):
        self.clicked = True

    def press(self, key):
        self.pressed = key


class _Page:
    def __init__(self):
        self.username = _Locator()
        self.password = _Locator()
        self.submit = _Locator()

    def locator(self, selector):
        return {
            "input[type=email]": self.username,
            "input[type=password]": self.password,
            "button[type=submit]": self.submit,
        }.get(selector, _Locator(False))

    def wait_for_load_state(self, **_kwargs):
        return None

    def wait_for_timeout(self, _milliseconds):
        return None


def test_saved_credentials_fill_and_submit_the_real_login_controls():
    page = _Page()
    with mock.patch.object(
        lidl_credentials_store,
        "get",
        return_value={"email": "ivan@example.com", "password": "Secret-123"},
    ):
        assert lidl_monitor._try_saved_login(page) is True
    assert page.username.value == "ivan@example.com"
    assert page.password.value == "Secret-123"
    assert page.submit.clicked is True


def test_autologin_does_nothing_without_complete_credentials():
    page = _Page()
    with mock.patch.object(
        lidl_credentials_store,
        "get",
        return_value={"email": "ivan@example.com", "password": ""},
    ):
        assert lidl_monitor._try_saved_login(page) is False
    assert page.username.value == ""
    assert page.password.value == ""
