"""Проверка подлинности авто-обновления (F28): разбор контрольной суммы и хэш файла.

Сеть НЕ трогаем и обновление НЕ запускаем — только чистые функции:
  - update_check._sha256_from_digest / _resolve_sha256 (какую сумму ждать);
  - desktop_app._norm_sha (валидна ли сумма) и _sha256_file (хэш скачанного).

Смысл F28: приложение не должно устанавливать архив, чья контрольная сумма не
совпала с ожидаемой из доверенного канала (GitHub API по https).

Запуск без зависимостей:  python tests/test_update_verify.py
"""
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import update_check
import desktop_app
from unittest import mock

HEX = "a" * 64
HEX2 = "b" * 64


def test_digest_sha256_parsed():
    assert update_check._sha256_from_digest({"digest": "sha256:" + HEX}) == HEX
    # регистр не важен — приводим к нижнему
    assert update_check._sha256_from_digest({"digest": "SHA256:" + "A" * 64}) == "a" * 64


def test_digest_bad_or_missing_is_empty():
    assert update_check._sha256_from_digest(None) == ""
    assert update_check._sha256_from_digest({}) == ""
    assert update_check._sha256_from_digest({"digest": "md5:" + HEX}) == ""
    assert update_check._sha256_from_digest({"digest": "sha256:zzz"}) == ""


def test_resolve_prefers_digest_no_network():
    # digest есть → берём его, к *.sha256 (сети) не ходим
    assert update_check._resolve_sha256({"digest": "sha256:" + HEX2}, []) == HEX2


def test_resolve_empty_when_nothing():
    # нет ни digest, ни *.sha256-ассета → пустая строка (установку делать нельзя)
    assert update_check._resolve_sha256(None, []) == ""


def test_resolve_uses_sidecar_for_selected_zip_only():
    zip_asset = {"name": "WexFlow-1.4.4.zip"}
    assets = [
        {"name": "WexFlow-Setup.exe.sha256", "browser_download_url": "https://bad/setup"},
        {"name": "WexFlow-1.4.4.zip.sha256", "browser_download_url": "https://good/zip"},
    ]
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = (HEX + "  WexFlow-1.4.4.zip\n").encode()
    with mock.patch.object(update_check.urllib.request, "urlopen", return_value=response) as opened:
        assert update_check._resolve_sha256(zip_asset, assets) == HEX
    assert opened.call_args.args[0].full_url == "https://good/zip"


def test_sidecar_does_not_accept_hash_embedded_in_arbitrary_text():
    zip_asset = {"name": "WexFlow-1.4.4.zip"}
    assets = [
        {"name": "WexFlow-1.4.4.zip.sha256", "browser_download_url": "https://good/zip"},
    ]
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = ("not-a-checksum " + HEX).encode()

    with mock.patch.object(update_check.urllib.request, "urlopen", return_value=response):
        assert update_check._resolve_sha256(zip_asset, assets) == ""


def test_check_ignores_unrelated_zip_asset():
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = (
        b'{"tag_name":"v999.0.0","html_url":"https://release.example/",'
        b'"assets":[{"name":"debug-symbols.zip",'
        b'"browser_download_url":"https://release.example/debug-symbols.zip",'
        b'"digest":"sha256:' + HEX.encode() + b'"}]}'
    )
    with mock.patch.object(update_check.urllib.request, "urlopen", return_value=response):
        info = update_check.check()

    assert info["url"] == "https://release.example/"
    assert info["sha256"] == ""


def test_select_zip_asset_prefers_archive_matching_release_tag():
    assets = [
        {"name": "WexFlow-1.4.3.zip"},
        {"name": "WexFlow-1.4.4.zip"},
    ]

    assert update_check._select_zip_asset(assets, "v1.4.4")["name"] == "WexFlow-1.4.4.zip"


def test_select_zip_asset_fails_closed_when_multiple_are_ambiguous():
    assets = [
        {"name": "WexFlow-alpha.zip"},
        {"name": "WexFlow-beta.zip"},
    ]

    assert update_check._select_zip_asset(assets, "v1.4.4") is None


def test_norm_sha_validation():
    assert desktop_app._norm_sha(HEX) == HEX
    assert desktop_app._norm_sha("  " + "A" * 64 + "  ") == "a" * 64  # трим + нижний регистр
    assert desktop_app._norm_sha("a" * 63) == ""      # короткая
    assert desktop_app._norm_sha("g" * 64) == ""      # не hex
    assert desktop_app._norm_sha(None) == ""
    assert desktop_app._norm_sha("") == ""


def test_sha256_file_matches_hashlib():
    data = b"WexFlow update payload \x00\x01\x02" * 5000
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "update.zip")
        with open(p, "wb") as f:
            f.write(data)
        import pathlib
        got = desktop_app._sha256_file(pathlib.Path(p))
    assert got == hashlib.sha256(data).hexdigest()


def test_mismatch_is_detected():
    # два разных содержимого → разные суммы; совпадение — только при равенстве
    import pathlib
    with tempfile.TemporaryDirectory() as d:
        good = pathlib.Path(d) / "good.zip"
        bad = pathlib.Path(d) / "bad.zip"
        good.write_bytes(b"authentic build")
        bad.write_bytes(b"tampered build")
        expected = desktop_app._sha256_file(good)
        assert desktop_app._sha256_file(bad) != expected      # подмену видим
        assert desktop_app._sha256_file(good) == expected     # честный архив проходит


def test_sha256_file_missing_returns_empty():
    import pathlib
    assert desktop_app._sha256_file(pathlib.Path("no-such-file-xyz.zip")) == ""


if __name__ == "__main__":
    tests = [
        test_digest_sha256_parsed,
        test_digest_bad_or_missing_is_empty,
        test_resolve_prefers_digest_no_network,
        test_resolve_empty_when_nothing,
        test_norm_sha_validation,
        test_sha256_file_matches_hashlib,
        test_mismatch_is_detected,
        test_sha256_file_missing_returns_empty,
    ]
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
