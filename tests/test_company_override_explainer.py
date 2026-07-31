"""Company-specific answers must explain how they differ from shared defaults."""
from pathlib import Path


def test_company_override_section_explains_defaults_overrides_and_consents():
    html = (Path(__file__).parents[1] / "templates" / "account.html").read_text(
        encoding="utf-8"
    )
    assert "Ответы выше — основа" in html
    assert "Настройки ниже — только поправки" in html
    assert "Это не факты о тебе и не замена ответов сверху" in html
    assert "Сохранить настройки компании" in html
