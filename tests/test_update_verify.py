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
