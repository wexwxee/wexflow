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
import threading
from pathlib import Path
from unittest import mock

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


def test_non_object_json_is_quarantined_as_corrupt():
    d = _fresh_tmp()
    (d / "profile.json").write_text('["not", "a", "profile"]', encoding="utf-8")
    assert isinstance(profile_store.load_profile(), dict)
    assert any(p.name.startswith("profile.corrupt-") for p in d.iterdir())


def test_transient_read_oserror_keeps_the_original_profile():
    d = _fresh_tmp()
    profile_store.save_profile({"first_name": "Original", "email": "keep@example.com"})
    path = d / "profile.json"
    original = path.read_bytes()
    real_read_text = Path.read_text

    def transient_error(current, *args, **kwargs):
        if current == path:
            raise OSError("temporary sharing violation")
        return real_read_text(current, *args, **kwargs)

    with mock.patch.object(Path, "read_text", transient_error):
        try:
            profile_store.load_profile()
        except OSError:
            pass
        else:
            raise AssertionError("transient I/O error was mistaken for a usable empty profile")

    assert path.exists()
    assert path.read_bytes() == original
    assert not any(p.name.startswith("profile.corrupt-") for p in d.iterdir())


def test_concurrent_profile_mutations_preserve_both_fields():
    _fresh_tmp()
    profile_store.save_profile({"country": "Denmark"})
    original_country = profile_store.load_profile()["country"]
    start = threading.Barrier(3)
    errors = []

    def worker(key, value):
        try:
            start.wait(timeout=3)
            profile_store.mutate_profile(lambda profile: profile.__setitem__(key, value))
        except BaseException as exc:  # collect thread failures in the main test
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("first_name", "Ivan")),
        threading.Thread(target=worker, args=("email", "ivan@example.com")),
    ]
    for thread in threads:
        thread.start()
    start.wait(timeout=3)
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    profile = profile_store.load_profile()
    assert profile["country"] == original_country
    assert profile["first_name"] == "Ivan"
    assert profile["email"] == "ivan@example.com"


def test_lidl_profile_scope_is_validated_and_legacy_yes_stays_local():
    assert profile_store.clean_answer("lidl_profile_scope", "international") == "international"
    assert profile_store.clean_answer("lidl_profile_scope", "country") == "country"
    assert profile_store.clean_answer("lidl_profile_scope", "applied_only") == "applied_only"
    assert profile_store.clean_answer("lidl_profile_scope", "anything_else") == ""
    migrated = profile_store.clean_profile({"profile_visible": "yes"})
    assert migrated["lidl_profile_scope"] == "country"
    assert profile_store.clean_profile(
        {"profile_visible": "yes", "lidl_profile_scope": ""}
    )["lidl_profile_scope"] == ""


def test_company_answers_require_consent_and_keep_legal_choices_local():
    profile = {
        "answer_reuse_consent": "yes",
        "citizenship": "Ukraine",
        "work_weekends": "yes",
        "lidl_newsletter": "yes",
        "lidl_profile_scope": "country",
    }
    other = profile_store.resolve_company_answers(profile, "Netto")
    assert other["citizenship"] == "Ukraine"
    assert other["work_weekends"] == "yes"
    assert other["lidl_newsletter"] == ""
    assert other["lidl_profile_scope"] == ""

    lidl = profile_store.resolve_company_answers(profile, "Lidl")
    assert lidl["lidl_newsletter"] == "yes"
    assert lidl["lidl_profile_scope"] == "country"

    denied = profile_store.resolve_company_answers(
        dict(profile, answer_reuse_consent="no"),
        "Netto",
    )
    assert denied["citizenship"] == ""
    assert denied["work_weekends"] == ""


def test_cleaning_does_not_freeze_shared_answers_inside_lidl_override():
    profile = profile_store.clean_profile({
        "answer_reuse_consent": "yes",
        "work_weekends": "yes",
    })
    assert "work_weekends" not in (
        profile.get("company_answer_overrides", {}).get("lidl", {}).get("answers", {})
    )
    profile["work_weekends"] = "no"
    assert profile_store.resolve_company_answers(profile, "Lidl")["work_weekends"] == "no"


def test_legacy_lidl_local_answers_survive_but_never_reach_other_employers():
    profile = profile_store.clean_profile({
        "answer_reuse_consent": "yes",
        "lidl_discovery": "LinkedIn",
        "lidl_referral_name": "Lars",
        "lidl_current_employee": "yes",
        "lidl_previous_employment": "Lidl Herlev, 2024",
        "relevant_health_condition": "none",
    })
    lidl = profile_store.resolve_company_answers(profile, "Lidl")
    netto = profile_store.resolve_company_answers(profile, "Netto")
    for key in (
        "lidl_discovery", "lidl_referral_name", "lidl_current_employee",
        "lidl_previous_employment", "relevant_health_condition",
    ):
        assert lidl[key]
        assert netto[key] == ""


def test_company_override_can_replace_or_disable_common_answers():
    profile = {
        "answer_reuse_consent": "yes",
        "citizenship": "Ukraine",
        "work_weekends": "yes",
        "company_answer_overrides": {
            "netto": {
                "label": "Netto",
                "inherit_defaults": "yes",
                "answers": {"work_weekends": "no"},
            },
            "ikea": {
                "label": "IKEA",
                "inherit_defaults": "no",
                "answers": {"citizenship": "Denmark"},
            },
        },
    }
    netto = profile_store.resolve_company_answers(profile, "Netto")
    assert netto["citizenship"] == "Ukraine"
    assert netto["work_weekends"] == "no"
    ikea = profile_store.resolve_company_answers(profile, "IKEA")
    assert ikea["citizenship"] == "Denmark"
    assert ikea["work_weekends"] == ""


def test_lidl_discovery_is_company_local_and_uses_exact_site_values():
    profile = {
        "answer_reuse_consent": "yes",
        "lidl_discovery": "Facebook",
        "company_answer_overrides": {
            "lidl": {
                "label": "Lidl",
                "inherit_defaults": "yes",
                "answers": {"lidl_discovery": "Lidls karriereside"},
            },
        },
    }
    assert profile_store.resolve_company_answers(profile, "Netto")["lidl_discovery"] == ""
    assert (
        profile_store.resolve_company_answers(profile, "Lidl")["lidl_discovery"]
        == "Lidls karriereside"
    )
    values = {value for value, _label in profile_store.LIDL_DISCOVERY_OPTIONS}
    assert {"Jobindex", "LinkedIn", "Messe", "Ungarbejder.dk"} <= values


def test_lidl_part_time_answer_is_reused_only_for_lidl():
    answer = "Den angivne ugentlige arbejdstid passer mig godt."
    profile = {
        "answer_reuse_consent": "yes",
        "company_answer_overrides": {
            "lidl": {
                "label": "Lidl",
                "inherit_defaults": "yes",
                "answers": {"lidl_part_time_availability": answer},
            },
        },
    }
    assert (
        profile_store.resolve_company_answers(profile, "Lidl Danmark")[
            "lidl_part_time_availability"
        ] == answer
    )
    assert (
        profile_store.resolve_company_answers(profile, "Netto")[
            "lidl_part_time_availability"
        ] == ""
    )


def test_long_lidl_goal_and_danmark_brand_keep_the_saved_company_answers():
    goal = (
        "Om to år ser jeg mig selv i gang med en businessuddannelse og med "
        "værdifuld praktisk erfaring fra Lidl. Jeg håber at have udviklet "
        "mit dansk og engelsk og lært mere om daglige forretningsprocesser."
    )
    profile = {
        "answer_reuse_consent": "yes",
        "company_answer_overrides": {
            "lidl": {
                "label": "Lidl",
                "inherit_defaults": "yes",
                "answers": {
                    "two_year_goal": goal,
                    "lidl_newsletter": "yes",
                    "lidl_profile_scope": "country",
                },
            },
        },
    }
    resolved = profile_store.resolve_company_answers(profile, "Lidl Danmark")
    assert resolved["two_year_goal"] == goal
    assert len(resolved["two_year_goal"]) > 120
    assert resolved["lidl_newsletter"] == "yes"
    assert resolved["lidl_profile_scope"] == "country"


if __name__ == "__main__":
    tests = [
        test_round_trip,
        test_atomic_no_tmp_left,
        test_backup_mirrors_last_good,
        test_corrupt_recovers_from_bak,
        test_corrupt_without_backup_does_not_crash,
        test_non_object_json_is_quarantined_as_corrupt,
        test_transient_read_oserror_keeps_the_original_profile,
        test_concurrent_profile_mutations_preserve_both_fields,
        test_lidl_profile_scope_is_validated_and_legacy_yes_stays_local,
        test_company_answers_require_consent_and_keep_legal_choices_local,
        test_company_override_can_replace_or_disable_common_answers,
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
