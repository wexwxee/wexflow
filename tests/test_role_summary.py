"""Карточка сохраняет оригинал вакансии, но кратко объясняет роль по-русски."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import labels


def test_common_danish_and_english_roles_are_explained():
    cases = {
        "Click & Collect leder i POWER": "Руководитель Click & Collect",
        "Studentermedhjælper til AI-drevet produktudvikling": "Студент-помощник",
        "Full stack-udvikler til Product Engineering": "Full-stack разработчик",
        "Senior Backend Engineer": "Backend-разработчик",
        "Sælger i POWER Frederiksberg": "Продавец-консультант",
        "Project Controller til internationale projekter": "Финансовый контролёр",
        "Forretningsanalytiker til Kommerciel Analyse": "Аналитик",
        "Vil du stå i spidsen for DI’s projektøkonomi?": "Проектные финансы",
    }
    for title, expected in cases.items():
        assert labels.role_summary(title) == expected


def test_category_is_safe_fallback_and_unknown_stays_empty():
    assert labels.role_summary("Unik intern titel", "finance") == "Финансы"
    assert labels.role_summary("Unik intern titel") == ""


if __name__ == "__main__":
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print(f"OK   {name}")
