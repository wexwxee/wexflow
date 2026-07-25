"""Safety and regression tests for the optional AI form filler."""
import os
import re
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright

from connectors import ai_fill, fill_common


def _page():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    return playwright, browser, browser.new_page()


def test_text_answers_are_exact_profile_values_not_model_text():
    fields = [{"key": "field", "label": "Municipality", "tag": "input"}]
    profile = {"city": "København", "about": "A narrative"}

    clean = ai_fill._validate(
        {"field": {"source": "city", "value": "HALLUCINATED"}},
        fields,
        profile,
    )

    assert clean == {"field": "København"}
    assert ai_fill._validate({"field": "HALLUCINATED"}, fields, profile) == {}
    assert ai_fill._validate(
        {"field": {"source": "about"}},
        fields,
        profile,
    ) == {}


def test_select_requires_existing_option_and_profile_source():
    fields = [{
        "key": "language",
        "label": "Preferred language",
        "tag": "select",
        "options": ["English", "Danish"],
    }]
    profile = {"languages": "English — fluent"}

    assert ai_fill._validate(
        {"language": {"source": "languages", "option": "english"}},
        fields,
        profile,
    ) == {"language": "English"}
    assert ai_fill._validate(
        {"language": {"source": "missing", "option": "English"}},
        fields,
        profile,
    ) == {}
    assert ai_fill._validate(
        {"language": {"source": "languages", "option": "Spanish"}},
        fields,
        profile,
    ) == {}


def test_profile_is_minimized_for_the_detected_fields():
    profile = {
        "city": "København",
        "email": "person@example.com",
        "date_of_birth": "1990-01-01",
        "about": "Three years in retail",
    }

    language_payload = ai_fill._profile_for_fields(
        profile,
        [{"label": "Preferred working language"}],
    )
    email_payload = ai_fill._profile_for_fields(
        profile,
        [{"label": "E-mail address"}],
    )

    assert language_payload == {"city": "København"}
    assert email_payload == {"city": "København", "email": "person@example.com"}
    assert "date_of_birth" not in email_payload
    assert "about" not in email_payload


def test_collection_excludes_narratives_and_rekeys_a_repeated_pass():
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <label for="city">Municipality</label><input id="city">
          <label for="years">Years of experience</label><input id="years">
          <label for="why">Why do you want to join us?</label><textarea id="why"></textarea>
          <label for="about">Tell us about yourself</label><input id="about">
          <input id="combo" role="combobox" aria-label="Language" aria-controls="language-list">
          <div id="language-list" role="listbox"><div role="option">English</div></div>
          <label for="selected">Already selected language</label>
          <select id="selected"><option>English</option><option>Danish</option></select>
        """)

        first = ai_fill._collect_open_fields(page)
        assert [field["label"] for field in first] == ["Municipality", "Years of experience"]
        assert len({field["key"] for field in first}) == 2

        page.locator("#city").fill("København")
        second = ai_fill._collect_open_fields(page)
        assert [field["label"] for field in second] == ["Years of experience"]
        second_key = second[0]["key"]
        assert page.locator("#city").get_attribute("data-wexflow-ai") is None
        assert page.locator(f'[data-wexflow-ai="{second_key}"]').get_attribute("id") == "years"
    finally:
        browser.close()
        playwright.stop()


def test_full_ordinary_pass_uses_profile_sources_and_real_options():
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <label for="city">Municipality</label><input id="city">
          <label for="language">Preferred working language</label>
          <select id="language">
            <option value="">Choose</option>
            <option value="en">English</option>
            <option value="da">Danish</option>
          </select>
        """)

        def fake_ask(prompt, **kwargs):
            pairs = re.findall(r'"key":\s*"([^"]+)",\s*"label":\s*"([^"]+)"', prompt)
            by_label = {label: key for key, label in pairs}
            return {"answers": {
                by_label["Municipality"]: {"source": "city"},
                by_label["Preferred working language"]: {
                    "source": "languages",
                    "option": "English",
                },
            }}

        with (
            mock.patch.object(ai_fill, "enabled", return_value=True),
            mock.patch.object(ai_fill, "available", return_value=True),
            mock.patch.object(ai_fill, "motivation_enabled", return_value=False),
            mock.patch.object(ai_fill, "_ask_gemini", side_effect=fake_ask),
        ):
            result = ai_fill.fill(
                page,
                {"city": "København", "languages": "English — fluent"},
            )

        assert page.locator("#city").input_value() == "København"
        assert page.locator("#language").input_value() == "en"
        assert {item["value"] for item in result} == {"København", "English"}
        assert all(item["kind"] == "filled" for item in result)
        run = page.evaluate("window.__wexflowAiRun")
        assert run["state"] == "done"
        assert run["step"] == run["total"] == 5
        assert run["percent"] == 100
    finally:
        browser.close()
        playwright.stop()


def test_combobox_options_and_click_are_scoped_to_aria_controls():
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <input id="language" role="combobox" aria-label="Language" aria-controls="language-list">
          <div id="language-list" role="listbox">
            <div role="option" onclick="language.value=this.textContent">English</div>
            <div role="option" onclick="language.value=this.textContent">Danish</div>
          </div>
          <input id="country" role="combobox" aria-label="Country" aria-controls="country-list">
          <div id="country-list" role="listbox">
            <div role="option" onclick="country.value=this.textContent">Denmark</div>
            <div role="option" onclick="country.value=this.textContent">Sweden</div>
          </div>
        """)

        assert ai_fill._collect_open_fields(page) == []
        combos = ai_fill._collect_comboboxes(page)
        country = next(item for item in combos if item["label"] == "Country")

        assert ai_fill._read_combo_options(page, country["key"]) == ["Denmark", "Sweden"]
        assert ai_fill._apply_combo(page, country["key"], "Sweden")
        assert page.locator("#country").input_value() == "Sweden"
        assert page.locator("#language").input_value() == ""
    finally:
        browser.close()
        playwright.stop()


def test_motivation_field_is_untouched_when_draft_toggle_is_off():
    playwright, browser, page = _page()
    try:
        page.set_content(
            '<label for="why">Why do you want to join us?</label><textarea id="why"></textarea>'
        )
        with (
            mock.patch.object(ai_fill, "enabled", return_value=True),
            mock.patch.object(ai_fill, "available", return_value=True),
            mock.patch.object(ai_fill, "motivation_enabled", return_value=False),
            mock.patch.object(ai_fill, "_ask_gemini") as ask,
        ):
            result = ai_fill.fill(page, {"about": "Three years in retail"})

        assert result == []
        assert page.locator("#why").input_value() == ""
        ask.assert_not_called()
    finally:
        browser.close()
        playwright.stop()


def test_motivation_is_one_explicit_full_length_draft_call():
    playwright, browser, page = _page()
    try:
        page.set_content("""
          <title>Store Assistant</title>
          <label for="why">Why do you want to join us?</label><textarea id="why"></textarea>
        """)
        draft = "I have three years of retail experience. " + ("Reliable team player. " * 8)
        calls = []

        def fake_ask(prompt, **kwargs):
            calls.append(prompt)
            key = re.search(r'"key":\s*"([^"]+-mot-\d+)"', prompt).group(1)
            return {"drafts": {key: draft}}

        with (
            mock.patch.object(ai_fill, "enabled", return_value=True),
            mock.patch.object(ai_fill, "available", return_value=True),
            mock.patch.object(ai_fill, "motivation_enabled", return_value=True),
            mock.patch.object(ai_fill, "_ask_gemini", side_effect=fake_ask),
        ):
            result = ai_fill.fill(page, {"about": "Three years in retail"})

        assert len(calls) == 1
        assert result == [{
            "label": "Why do you want to join us?",
            "value": draft.strip(),
            "kind": "draft",
        }]
        assert page.locator("#why").input_value() == draft.strip()
    finally:
        browser.close()
        playwright.stop()


def test_gemini_fallback_is_bounded_to_two_models():
    response = mock.Mock(status_code=429)
    with (
        mock.patch.object(ai_fill.ai_filters, "api_key", return_value="test-key"),
        mock.patch.object(
            ai_fill.ai_filters,
            "_models_to_try",
            return_value=["one", "two", "three", "four"],
        ),
        mock.patch.object(ai_fill.httpx, "post", return_value=response) as post,
    ):
        assert ai_fill._ask_gemini("prompt") is None

    assert post.call_count == 2
    assert all(call.kwargs["timeout"] <= ai_fill._REQUEST_TIMEOUT_SECONDS for call in post.call_args_list)


def test_banner_shows_the_complete_draft_value():
    playwright, browser, page = _page()
    try:
        page.set_content("<main>Employer form</main>")
        draft = "Start " + ("details " * 30) + "END"
        fill_common.show_ai_progress(
            page, 5, 5, "Form ready", "Checked all stages", state="done",
        )
        fill_common.add_banner(
            page,
            0,
            ["motivation (черновик)"],
            ai_details=[{"label": "Why us?", "value": draft, "kind": "draft"}],
        )

        assert draft in page.locator("#wexflow-banner").locator(".ai").inner_text()
        assert "5" in page.locator("#wexflow-banner").locator(".ai-run").inner_text()
    finally:
        browser.close()
        playwright.stop()
