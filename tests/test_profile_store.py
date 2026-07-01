"""Надёжность profile_store (остаток F37; тот же инвариант, что settings_store/F33).

Проверяем на ВРЕМЕННЫХ путях (реальный profile.json не трогаем):
  - запись атомарная — после save нет хвоста .tmp;
  - .bak зеркалит последний успешно записанный профиль;
  - битый основной файл НЕ роняет приложение: восстанавливаемся из .bak,
    а сам битый файл откладываем как .corrupt-<ts>;
  - битый файл без .bak — тоже не падаем, стартуем с чистого профиля.

Запуск без зависимостей:  python tests/test_profile_store.py
Или через pytest:         pytest tests/test_profile_store.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import profile_store


def _fresh_tmp() -> Path:
    """Свежая временная папка + перенаправление путей config на неё."""
    d = Path(tempfile.mkdtemp(prefix="wex_profile_"))
    config.SHARED_PROFILE_PATH = d / "profile.json"
    config.PROFILE_PATH = d / "legacy.json"   # не существует → миграция ничего не делает
    config.BASE_DIR = d                        # нет profile.example.json → чистый fallback
    return d


def test_round_trip():
    _fresh_tmp()
    profile_store.save_profile({"first_name": "Иван", "email": "a@b.dk"})
    got = profile_store.load_profile()
    assert got.get("first_name") == "Иван"
    assert got.get("email") == "a@b.dk"


def test_atomic_no_tmp_left():
    d = _fresh_tmp()
    profile_store.save_profile({"first_name": "A"})
    assert not (d / "profile.json.tmp").exists(), "остался временный файл .tmp"


def test_backup_mirrors_last_good():
    d = _fresh_tmp()
    profile_store.save_profile({"first_name": "A"})
    profile_store.save_profile({"first_name": "B"})
    bak = d / "profile.json.bak"
    assert bak.exists(), ".bak не создан"
    assert json.loads(bak.read_text(encoding="utf-8")).get("first_name") == "B"


def test_corrupt_recovers_from_bak():
    d = _fresh_tmp()
    profile_store.save_profile({"first_name": "A"})
    profile_store.save_profile({"first_name": "B"})        # .bak = B
    (d / "profile.json").write_text("{ битый JSON", encoding="utf-8")  # портим основной
    got = profile_store.load_profile()
    assert got.get("first_name") == "B", "не восстановился из .bak"
    # основной файл снова валиден и равен B
    assert json.loads((d / "profile.json").read_text(encoding="utf-8")).get("first_name") == "B"
    # битый отложен как profile.corrupt-<ts>.json
    assert any(p.name.startswith("profile.corrupt-") for p in d.iterdir()), "битый файл не отложен"


def test_corrupt_without_backup_does_not_crash():
    d = _fresh_tmp()
    (d / "profile.json").write_text("совсем не JSON", encoding="utf-8")  # битый, .bak нет
    got = profile_store.load_profile()
    assert isinstance(got, dict), "загрузка упала вместо чистого профиля"
    assert got.get("first_name", "") == ""
    assert any(p.name.startswith("profile.corrupt-") for p in d.iterdir()), "битый файл не отложен"


if __name__ == "__main__":
    tests = [
        test_round_trip,
        test_atomic_no_tmp_left,
        test_backup_mirrors_last_good,
        test_corrupt_recovers_from_bak,
        test_corrupt_without_backup_does_not_crash,
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
