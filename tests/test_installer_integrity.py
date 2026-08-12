from __future__ import annotations

import hashlib
from unittest import mock

import pytest

from installer import installer


def test_expected_sha_prefers_github_digest_without_network():
    digest = "a" * 64
    asset = {"name": "WexFlow-1.4.4.zip", "digest": "sha256:" + digest}

    with mock.patch.object(installer.urllib.request, "urlopen") as opened:
        assert installer._expected_sha256(asset, [asset]) == digest

    opened.assert_not_called()


def test_expected_sha_reads_only_matching_zip_sidecar():
    digest = "b" * 64
    zip_asset = {"name": "WexFlow-1.4.4.zip"}
    assets = [
        {"name": "WexFlow-Setup.exe.sha256", "browser_download_url": "https://bad/setup"},
        {"name": "WexFlow-1.4.4.zip.sha256", "browser_download_url": "https://good/zip"},
    ]
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = (digest + "  archive.zip\n").encode()

    with mock.patch.object(installer.urllib.request, "urlopen", return_value=response) as opened:
        assert installer._expected_sha256(zip_asset, assets) == digest

    assert opened.call_args.args[0].full_url == "https://good/zip"


def test_sha256_file_matches_payload(tmp_path):
    payload = b"verified installer payload"
    path = tmp_path / "release.zip"
    path.write_bytes(payload)

    assert installer._sha256_file(str(path)) == hashlib.sha256(payload).hexdigest()


def test_expected_sha_empty_sidecar_fails_closed_without_index_error():
    zip_asset = {"name": "WexFlow-1.4.4.zip"}
    assets = [
        {"name": "WexFlow-1.4.4.zip.sha256", "browser_download_url": "https://good/zip"},
    ]
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = b"   \n"

    with mock.patch.object(installer.urllib.request, "urlopen", return_value=response):
        assert installer._expected_sha256(zip_asset, assets) == ""


def test_select_zip_asset_prefers_archive_matching_release_tag():
    assets = [
        {"name": "WexFlow-1.4.3.zip"},
        {"name": "WexFlow-1.4.4.zip"},
    ]

    selected = installer._select_zip_asset(assets, "v1.4.4")
    assert selected["name"] == "WexFlow-1.4.4.zip"


def test_select_zip_asset_rejects_ambiguous_fallback():
    assets = [
        {"name": "WexFlow-alpha.zip"},
        {"name": "WexFlow-beta.zip"},
    ]

    assert installer._select_zip_asset(assets, "v1.4.4") is None


def test_do_install_hash_mismatch_never_installs_and_removes_download(tmp_path):
    downloaded = tmp_path / "WexFlow-1.4.4.zip"

    def fake_download(_url, filename, reporthook=None):
        assert filename == str(downloaded)
        downloaded.write_bytes(b"tampered payload")
        return filename, None

    with (
        mock.patch.object(installer, "ensure_dotnet48"),
        mock.patch.object(installer, "ensure_webview2"),
        mock.patch.object(
            installer,
            "latest_zip",
            return_value=(
                "https://example.invalid/WexFlow-1.4.4.zip",
                downloaded.name,
                "v1.4.4",
                "a" * 64,
            ),
        ),
        mock.patch.object(installer.tempfile, "gettempdir", return_value=str(tmp_path)),
        mock.patch.object(installer.urllib.request, "urlretrieve", side_effect=fake_download),
        mock.patch.object(installer, "_sha256_file", return_value="b" * 64),
        mock.patch.object(installer.os, "makedirs") as make_dirs,
        mock.patch.object(installer, "install_from_zip") as install,
    ):
        with pytest.raises(RuntimeError, match="SHA-256 не совпадает"):
            installer.do_install(mock.Mock())

    make_dirs.assert_not_called()
    install.assert_not_called()
    assert not downloaded.exists()
