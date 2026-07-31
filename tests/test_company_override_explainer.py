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


def test_lidl_discovery_is_an_exact_company_only_select():
    html = (Path(__file__).parents[1] / "templates" / "account.html").read_text(
        encoding="utf-8"
    )
    assert html.count('name="lidl_discovery"') == 1
    assert 'data-company-only="lidl"' in html
    assert "Это не свободный текст" in html
    assert "{% for value, russian in lidl_discovery_options %}" in html
