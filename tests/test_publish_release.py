from pathlib import Path
from unittest import mock

import publish_release


def test_release_notes_use_current_changelog():
    notes = publish_release._release_notes("1.3.14")

    assert notes.startswith("WexFlow 1.3.14")
    assert "системная тень frameless-окна" in notes
    assert "WexFlow-Setup.exe" in notes


def test_release_notes_include_exact_hashes_and_scan_link():
    notes = publish_release._release_notes(
        "1.3.14",
        hashes={"WexFlow-1.3.14.zip": "a" * 64, "WexFlow-Setup.exe": "b" * 64},
        virustotal_url="https://www.virustotal.com/gui/file/example",
    )

    assert "`WexFlow-1.3.14.zip`: `" + "a" * 64 + "`" in notes
    assert "`WexFlow-Setup.exe`: `" + "b" * 64 + "`" in notes
    assert "https://www.virustotal.com/gui/file/example" in notes


def test_write_sha256_creates_matching_sidecar(tmp_path: Path):
    asset = tmp_path / "asset.bin"
    asset.write_bytes(b"release payload")

    digest, sidecar = publish_release._write_sha256(asset)

    assert len(digest) == 64
    assert sidecar.read_text(encoding="ascii") == f"{digest}  asset.bin\n"


def test_files_match_hashes_detects_asset_changed_after_scan(tmp_path: Path):
    asset = tmp_path / "WexFlow-Setup.exe"
    asset.write_bytes(b"scanned payload")
    digest = publish_release._sha256_of(asset)

    assert publish_release._files_match_hashes({asset: digest}) is True
    asset.write_bytes(b"replaced after scan")
    assert publish_release._files_match_hashes({asset: digest}) is False


def test_virustotal_marker_must_match_exact_installer_hash(tmp_path: Path):
    setup = tmp_path / "WexFlow-Setup.exe"
    setup.write_bytes(b"exact release installer")
    digest = publish_release._sha256_of(setup)
    marker = tmp_path / "WexFlow-Setup.exe.virustotal.txt"
    exact_url = f"https://www.virustotal.com/gui/file/{digest}/detection"
    marker.write_text(exact_url + "\n", encoding="utf-8")

    assert publish_release._virustotal_url(setup, digest) == exact_url


def test_virustotal_marker_rejects_stale_hash_and_lookalike_host(tmp_path: Path):
    setup = tmp_path / "WexFlow-Setup.exe"
    setup.write_bytes(b"new installer")
    digest = publish_release._sha256_of(setup)
    marker = tmp_path / "WexFlow-Setup.exe.virustotal.txt"

    marker.write_text(
        f"https://www.virustotal.com/gui/file/{'0' * 64}/detection\n",
        encoding="utf-8",
    )
    assert publish_release._virustotal_url(setup, digest) == ""

    marker.write_text(
        f"https://www.virustotal.com.evil.example/gui/file/{digest}/detection\n",
        encoding="utf-8",
    )
    assert publish_release._virustotal_url(setup, digest) == ""


def test_release_refuses_changed_tracked_sources():
    clean = mock.Mock(returncode=0)
    dirty = mock.Mock(returncode=1)

    with mock.patch.object(
        publish_release.subprocess,
        "run",
        side_effect=[dirty, clean],
    ):
        assert publish_release._tracked_worktree_clean() is False


def test_release_accepts_clean_tracked_sources():
    clean = mock.Mock(returncode=0)

    with mock.patch.object(
        publish_release.subprocess,
        "run",
        side_effect=[clean, clean],
    ):
        assert publish_release._tracked_worktree_clean() is True
