from unittest import mock

import publish_release


def test_release_notes_use_current_changelog():
    notes = publish_release._release_notes("1.3.14")

    assert notes.startswith("WexFlow 1.3.14")
    assert "системная тень frameless-окна" in notes
    assert "WexFlow-Setup.exe" in notes


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
