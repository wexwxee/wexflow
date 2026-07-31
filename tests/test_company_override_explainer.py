"""Company-specific answers must explain how they differ from shared defaults."""
from pathlib import Path


def test_company_override_section_explains_defaults_overrides_and_consents():
    html = (Path(__file__).parents[1] / "templates" / "account.html").read_text(
        encoding="utf-8"
    )
    assert "Настройки компаний" in html
    assert "Здесь нет отдельного режима «исключений»" in html
    assert "Вопросы анкеты Lidl" in html
    assert "Согласия Lidl" in html
    assert "Сохранить настройки выбранной компании" in html
    assert "firstSavedCompany" in html


def test_lidl_discovery_is_an_exact_company_only_select():
    html = (Path(__file__).parents[1] / "templates" / "account.html").read_text(
        encoding="utf-8"
    )
    assert html.count('name="lidl_discovery"') == 1
    assert 'data-company-only="lidl"' in html
    assert "свободный текст сюда не отправляется" in html
    assert "{% for value, russian in lidl_discovery_options %}" in html
