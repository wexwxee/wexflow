"""Filler for Lidl Denmark's SAP UI5 EasyApply form.

Only profile facts, stored answers and documents are inserted — nothing is ever
invented. Screening questions are answered from the profile answer bank
(«Ответы для анкет»); a question with no stored answer stays empty and blocks
the automatic submit instead of being guessed.

Two safety gates before anything is typed or clicked:
  * the form contract (connectors.site_contract) must still match — a redesigned
    Lidl form stops the run with a human message instead of blind filling;
  * the automatic submit runs only when every required answer is present and
    Lidl's own button is enabled. Otherwise the armed green WexFlow button stays
    for the human.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import form_questions
import profile_store
from connectors import site_contract
from connectors.fill_common import add_banner, dismiss_cookies


def normalize_phone(value: str) -> str:
    """Use the international 00-prefix required by the Danish Lidl form."""
    raw = re.sub(r"[\s().-]+", "", str(value or "").strip())
    if raw.startswith("+"):
        return "00" + raw[1:]
    if raw.isdigit() and len(raw) == 8:
        return "0045" + raw
    return raw


def split_address(value: str) -> tuple[str, str]:
    """Split a Danish one-line address into street name and house number.

    Lidl asks for «Gade» and «Husnummer» separately, while the WexFlow profile
    keeps one line. Floor/door details after a comma stay out of both fields —
    the house-number input only accepts six characters.
    """
    line = str(value or "").split(",")[0].strip()
    match = re.match(r"^(.*?)[\s.]+(\d+\s*[A-Za-zÆØÅæøå]?)$", line)
    if not match:
        return line, ""
    street = match.group(1).strip(" .,")
    number = re.sub(r"\s+", "", match.group(2))
    if not street:
        return line, ""
    return street, number[:6]


def _caption_input_id(page, caption: str) -> str | None:
    """Find the input that a bare «Gade:»-style caption belongs to.

    These four address captions are rendered as plain sap.m.Label spans without
    a `for` attribute, so get_by_label cannot see them. The input that follows
    the caption in document order is the right one — but only when it carries no
    label of its own, otherwise we would grab the next real question instead.
    """
    try:
        return page.evaluate(
            """(caption) => {
                const wanted = caption.trim().toLowerCase();
                const labels = [...document.querySelectorAll('.sapMLabel')];
                const target = labels.find(el =>
                    (el.innerText || '').trim().replace(/[:*\\s]+$/, '').toLowerCase() === wanted);
                if (!target) return null;
                const all = [...document.querySelectorAll('*')];
                const start = all.indexOf(target);
                for (let i = start + 1; i < all.length && i < start + 40; i++) {
                    const node = all[i];
                    if (node.tagName !== 'INPUT') continue;
                    if (!node.classList.contains('sapMInputBaseInner')) continue;
                    if (node.getAttribute('aria-labelledby')) return null;
                    return node.id || null;
                }
                return null;
            }""",
            caption,
        )
    except Exception:
        return None


def _fill_caption(page, caption: str, value: str) -> bool:
    """Fill an address field addressed only by its visible caption."""
    value = str(value or "").strip()
    if not value:
        return False
    control_id = _caption_input_id(page, caption)
    if not control_id:
        return False
    try:
        control = page.locator(f"#{control_id}")
        if (control.count() and control.is_visible() and control.is_editable()
                and not (control.input_value() or "").strip()):
            control.fill(value)
            return True
    except Exception:
        pass
    return False


def _fill_labeled(page, label: str | tuple[str, ...], value: str) -> bool:
    value = str(value or "").strip()
    if not value:
        return False
    variants = tuple(label) if isinstance(label, (tuple, list)) else (label,)
    for variant in variants:
        try:
            control = page.get_by_label(re.compile(re.escape(variant), re.I)).first
            if (control.count() and control.is_visible() and control.is_editable()
                    and not (control.input_value() or "").strip()):
                control.fill(value)
                return True
        except Exception:
            pass
    return False


def _select_ui5(page, label: str, value: str) -> bool:
    """Select a value from a sap.m.Select associated with a visible label."""
    value = str(value or "").strip()
    if not value:
        return False
    aliases = {value.casefold()}
    if value.casefold() in {"danmark", "denmark", "dk"}:
        aliases.update({"danmark", "denmark"})
    try:
        labels = page.locator("label").all()
        wanted_label = label.casefold()
        matching_label = next(
            (
                candidate for candidate in labels
                if (candidate.inner_text() or "").strip().casefold() == wanted_label
            ),
            None,
        )
        if matching_label is None:
            matching_label = next(
                (
                    candidate for candidate in labels
                    if (candidate.inner_text() or "").strip().casefold().startswith(
                        wanted_label
                    )
                ),
                None,
            )
        if matching_label is None:
            return False
        label_id = matching_label.get_attribute("id") or ""
        target_id = matching_label.get_attribute("for") or ""
        controls = page.locator(
            f'[role="combobox"][aria-labelledby="{label_id}"]'
            if label_id else '[role="combobox"]'
        )
        control = controls.first
        if not control.count() and target_id.endswith("-hiddenInput"):
            control = page.locator(f"#{target_id.removesuffix('-hiddenInput')}-hiddenSelect")
        if not control.count() or not control.is_visible():
            return False
        click_target = control
        if (control.get_attribute("id") or "").endswith("-hiddenSelect"):
            click_target = control.locator("xpath=..")
        click_target.click()
        page.wait_for_timeout(150)
        options = page.locator('[role="option"], .sapMSelectListItem').all()
        for option in options:
            text = (option.inner_text() or "").strip().casefold()
            if option.is_visible() and text in aliases:
                option.click()
                return True
        page.keyboard.press("Escape")
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    return False


def _set_switch_by_text(page, label_text: str | tuple[str, ...], enabled: bool) -> bool:
    """Set a UI5 switch next to visible text with a trusted browser click.

    SAP UI5 ignores a plain DOM ``element.click()`` for this switch in the real
    EasyApply form.  Locate it in the DOM, then let Playwright perform the same
    trusted pointer action as the person would.
    """
    label_variants = (
        tuple(label_text) if isinstance(label_text, (tuple, list)) else (label_text,)
    )
    try:
        target_id = page.evaluate(
            """(labelTexts) => {
                const wanted = labelTexts.map(text =>
                    String(text || '').toLocaleLowerCase('da-DK')
                ).filter(Boolean);
                const labels = [...document.querySelectorAll(
                    '.talentPoolText, .sapMText, label, .sapMLabel, span, p'
                )].filter(node => {
                    const own = (node.innerText || '').trim().toLocaleLowerCase('da-DK');
                    return wanted.some(text => own.includes(text));
                }).sort((a, b) =>
                    (a.innerText || '').length - (b.innerText || '').length
                );
                for (const label of labels) {
                    let row = label;
                    for (let depth = 0; row && depth < 7; depth++, row = row.parentElement) {
                        const control = row.querySelector(
                            '[role="switch"], .sapMSwt, input[type="checkbox"]'
                        );
                        if (!control) continue;
                        if (!control.id) {
                            control.setAttribute('data-wexflow-switch-target', 'true');
                            return '[data-wexflow-switch-target="true"]';
                        }
                        return '#' + CSS.escape(control.id);
                    }
                }
                return '';
            }""",
            label_variants,
        )
        if not target_id:
            return False
        control = page.locator(target_id).first
        if not control.count() or not control.is_visible():
            return False

        def checked() -> bool:
            return bool(control.evaluate(
                """node => node.matches(':checked')
                    || node.getAttribute('aria-checked') === 'true'
                    || node.classList.contains('sapMSwtOn')
                    || Boolean(node.querySelector('.sapMSwtOn, input:checked'))"""
            ))

        if checked() != bool(enabled):
            control.click()
            page.wait_for_timeout(150)
        return checked() == bool(enabled)
    except Exception:
        return False


def _upload(page, selector: str, path: str, role: str = "document") -> str:
    """Attach a document and return the exact basename exposed to Lidl."""
    path = str(path or "").strip()
    if not path or not Path(path).is_file():
        return ""
    try:
        path = profile_store.safe_document_upload_path(path, role)
        control = page.locator(selector).first
        if control.count():
            control.set_input_files(path)
            return Path(path).name
    except Exception:
        pass
    return ""


def selected_file_name(page, selector: str) -> str:
    """Return the basename actually attached to a browser file input."""
    try:
        control = page.locator(selector).first
        if not control.count():
            return ""
        return str(control.evaluate(
            "node => node.files && node.files[0] ? node.files[0].name : ''"
        ) or "").strip()
    except Exception:
        return ""


def _wait_for_picker_to_close(page) -> None:
    """Let the country popover finish closing before the summary banner shows."""
    try:
        page.wait_for_selector('[role="option"]:visible', state="hidden", timeout=3000)
    except Exception:
        pass


def _screening_question_count(page) -> int:
    try:
        return page.locator('label[for^="__group"]').count()
    except Exception:
        return 0


# ── Ответы из профиля ──────────────────────────────────────────────────────
# Вопрос анкеты узнаём по ключевым словам и отвечаем СОХРАНЁННЫМ ответом.
# Не узнали вопрос или ответа нет — оставляем пустым: выдумывать за человека
# нельзя, а незаполненный вопрос честно останавливает автоподачу.
# Правила «какой вопрос закрывается каким ответом профиля» живут в
# form_questions: ими пользуется и интерфейс, чтобы не спрашивать дважды.

_GENDER_LABELS = {
    "male": ("Mand", "Male", "Mænd"),
    "female": ("Kvinde", "Female", "Kvinder"),
    "other": ("Andet", "Other", "Non-binary"),
    "prefer_not_say": (
        "Ønsker ikke at oplyse",
        "Prefer not to say",
        "Vil ikke oplyse",
    ),
}

_YES_RE = re.compile(r"^\s*(ja|yes)\s*$", re.I)
_NO_RE = re.compile(r"^\s*(nej|no)\s*$", re.I)


def question_answer(text: str, answers: dict, *, allow_saved: bool = True) -> tuple[str, str]:
    """(ключ ответа, «yes»/«no»/'') для текста вопроса анкеты.

    Сначала смотрим личный банк ответов: там лежит то, что человек ответил
    ИМЕННО на этот вопрос в приложении. Не нашли — пробуем узнать вопрос по
    ключевым словам и взять ответ из профиля. Не узнали — пусто, и подача
    остановится: выдумывать за человека нельзя.
    """
    clean = str(text or "")
    if allow_saved:
        saved = form_questions.answer_for(clean)
        if saved in {"yes", "no"}:
            return "saved", saved
    key = form_questions.profile_key_for(clean)
    if key:
        return key, str(answers.get(key) or "")
    return "", ""


def _radio_groups(page) -> list[dict]:
    """Вопросы с Ja/Nej: текст вопроса + идентификаторы обеих кнопок.

    Читаем структуру страницы одним проходом — так и быстрее, и не зависим от
    того, как именно UI5 расставил вложенность в конкретном релизе.
    """
    try:
        return page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll('.sapMRbG, [role="radiogroup"]').forEach((group, i) => {
                    const items = [...group.querySelectorAll('.sapMRb, [role="radio"]')];
                    if (!items.length) return;
                    let question = '';
                    const labelled = group.getAttribute('aria-labelledby');
                    if (labelled) {
                        question = labelled.split(/\\s+/)
                            .map(id => (document.getElementById(id) || {}).innerText || '')
                            .join(' ').trim();
                    }
                    if (!question) {
                        const row = group.closest('.sapUiFormElement, .sapMFlexBox, tr, div');
                        question = row ? (row.innerText || '').trim() : '';
                    }
                    const options = items.map(item => ({
                        id: item.id || '',
                        text: (item.innerText || '').trim(),
                        checked: item.getAttribute('aria-checked') === 'true'
                            || Boolean(item.querySelector('input:checked')),
                    }));
                    out.push({ index: i, id: group.id || '', question, options });
                });
                return out;
            }"""
        )
    except Exception:
        return []


def _click_option(page, option_id: str) -> bool:
    if not option_id:
        return False
    try:
        control = page.locator(f"#{option_id}")
        if control.count() and control.is_visible():
            control.click()
            return True
    except Exception:
        pass
    return False


def _choose_radio_text(
    page,
    question_fragment: str | tuple[str, ...],
    option_fragments: tuple[str, ...],
) -> bool:
    """Choose a saved non-binary radio answer by its visible Danish wording."""
    question_wanted = tuple(
        part.casefold()
        for part in (
            question_fragment
            if isinstance(question_fragment, (tuple, list))
            else (question_fragment,)
        )
    )
    option_wanted = tuple(part.casefold() for part in option_fragments)
    for group in _radio_groups(page):
        question = str(group.get("question") or "").casefold()
        if not any(fragment in question for fragment in question_wanted):
            continue
        options = list(group.get("options") or [])
        if any(option.get("checked") for option in options):
            return True
        for option in options:
            text = str(option.get("text") or "").casefold()
            if any(fragment in text for fragment in option_wanted):
                return _click_option(page, str(option.get("id") or ""))

    # Lidl's profile-visibility choices are a single-selection table, not a
    # radiogroup.  Each visible option text is in one row and its radio lives in
    # the selection cell of that same row.
    try:
        target_id = page.evaluate(
            """(wanted) => {
                const nodes = [...document.querySelectorAll(
                    '.visibility-option, .sapMText, td, label, span, p'
                )].filter(node => {
                    const text = (node.innerText || '').trim().toLocaleLowerCase('da-DK');
                    return wanted.some(fragment => text.includes(fragment));
                }).sort((a, b) =>
                    (a.innerText || '').length - (b.innerText || '').length
                );
                for (const node of nodes) {
                    let row = node.closest('tr, [role="row"], .sapMLIB');
                    if (!row) {
                        row = node;
                        for (let depth = 0; row && depth < 7; depth++, row = row.parentElement) {
                            if (row.querySelector('[role="radio"], .sapMRb, input[type="radio"]')) {
                                break;
                            }
                        }
                    }
                    if (!row) continue;
                    const control = row.querySelector(
                        '[role="radio"], .sapMRb, input[type="radio"]'
                    );
                    if (!control) continue;
                    if (!control.id) {
                        control.setAttribute('data-wexflow-radio-target', 'true');
                        return '[data-wexflow-radio-target="true"]';
                    }
                    return '#' + CSS.escape(control.id);
                }
                return '';
            }""",
            option_wanted,
        )
        if not target_id:
            return False
        control = page.locator(target_id).first
        if not control.count() or not control.is_visible():
            return False
        checked = (
            control.get_attribute("aria-checked") == "true"
            or bool(control.locator("input:checked").count())
        )
        if not checked:
            control.click()
            page.wait_for_timeout(150)
        return (
            control.get_attribute("aria-checked") == "true"
            or bool(control.locator("input:checked").count())
        )
    except Exception:
        return False


def fill_answers(page, profile: dict) -> dict:
    """Ответить на вопросы Lidl сохранёнными ответами человека.

    Возвращает отчёт: что заполнено и какие вопросы остались без ответа
    (по ним автоподача не пойдёт).
    """
    answers = profile_store.answers(profile)
    filled: list[str] = []
    unanswered: list[str] = []

    # Всё, что спросил магазин, попадает в банк вопросов приложения: человек
    # ответит один раз, и следующая такая анкета заполнится сама.
    try:
        form_questions.record(
            [
                {
                    "text": str(group.get("question") or "").strip(),
                    "options": [str(o.get("text") or "") for o in (group.get("options") or [])],
                }
                for group in _radio_groups(page)
            ],
            source=str(profile.get("_job_source") or "lidl"),
            store_label=str(profile.get("_job_brand") or "Lidl"),
            role_kind=str(profile.get("_job_role_kind") or "regular"),
            job_title=str(profile.get("_job_title") or ""),
        )
    except Exception:
        pass

    gender_value = answers.get("gender") or ""
    if gender_value:
        for variant in _GENDER_LABELS.get(gender_value, ()):
            if _select_ui5(page, "Køn", variant):
                filled.append("пол")
                break

    start = str(answers.get("start_date") or "").strip()
    if start:
        parts = start.split("-")
        human_date = f"{parts[2]}.{parts[1]}.{parts[0]}" if len(parts) == 3 else start
        for label in (
            "Hvornår kan du tidligst påbegynde dit ansættelsesforhold hos os",
            "startdato",
            "Startdato",
        ):
            if _fill_labeled(page, label, human_date):
                filled.append("дата выхода")
                break

    text_fields = (
        (
            "Blev du henvist til Lidl af en nuværende Lidl-medarbejder",
            answers.get("lidl_referral_name"),
            "рекомендация сотрудника Lidl",
        ),
        (
            "Hvis du tidligere har været ansat i Lidl",
            answers.get("lidl_previous_employment"),
            "предыдущая работа в Lidl",
        ),
        (
            (
                "Stillingen er på deltid",
                "Hvordan passer det dig",
                "The position is part-time",
            ),
            answers.get("lidl_part_time_availability"),
            "подходящий неполный график Lidl",
        ),
        (
            "Noter venligst, hvis du lider af sygdomme",
            answers.get("relevant_health_condition"),
            "сведения о здоровье",
        ),
        (
            (
                "Hvor ser du dig selv om to år",
                "Кем вы видите себя через два года",
                "Где вы видите себя через два года",
                "Where do you see yourself in two years",
            ),
            answers.get("two_year_goal"),
            "цель на два года",
        ),
    )
    for label, value, human in text_fields:
        if _fill_labeled(page, label, value or ""):
            filled.append(human)

    select_fields = (
        (
            "Er du allerede ansat i Lidl",
            answers.get("lidl_current_employee"),
            "уже работает в Lidl",
        ),
        (
            "Hvordan har du fået kendskab til denne stilling",
            answers.get("lidl_discovery"),
            "источник вакансии",
        ),
        (
            "Hvad er dit statsborgerskab",
            answers.get("citizenship"),
            "гражданство",
        ),
        (
            "Har du en gyldig opholds-/arbejdstilladelse",
            answers.get("work_permit"),
            "разрешение на работу",
        ),
        (
            "Kan du fremvise en ren straffeattest",
            answers.get("clean_criminal_record"),
            "справка о несудимости",
        ),
    )
    for label, value, human in select_fields:
        actual = {"yes": "Ja", "no": "Nej"}.get(str(value or ""), value or "")
        if _select_ui5(page, label, actual):
            filled.append(human)

    newsletter = str(answers.get("lidl_newsletter") or "")
    if newsletter and _set_switch_by_text(
        page,
        (
            "Jeg vil vide mere om relevante stillinger",
            "Я хочу узнать больше о соответствующих вакансиях",
            "I would like to know more about relevant vacancies",
        ),
        newsletter == "yes",
    ):
        filled.append("новости Lidl")

    profile_scope = str(answers.get("lidl_profile_scope") or "")
    scope_options = {
        "international": ("Lidl International",),
        "country": ("mit bopælsland", "стране моего проживания", "country of residence"),
        "applied_only": (
            "stillinger, jeg selv har søgt",
            "должности, на которые я подал",
            "должности, на которые я подала",
            "positions i have applied",
        ),
    }
    if profile_scope and _choose_radio_text(
        page,
        (
            "Min profil må gerne tages i betragtning",
            "Пожалуйста, ознакомьтесь с моим профилем",
            "Please consider my profile",
        ),
        scope_options.get(profile_scope, ()),
    ):
        filled.append("область учёта профиля Lidl")

    for group in _radio_groups(page):
        question = str(group.get("question") or "").strip()
        options = list(group.get("options") or [])
        if any(option.get("checked") for option in options):
            continue
        yes = next((o for o in options if _YES_RE.match(str(o.get("text") or ""))), None)
        no = next((o for o in options if _NO_RE.match(str(o.get("text") or ""))), None)
        if not yes or not no:
            unanswered.append(question[:120] or "вопрос без подписи")
            continue                      # не «да/нет» — без сохранённого выбора не трогаем
        key, answer = question_answer(
            question,
            answers,
            allow_saved=bool(profile.get("_allow_shared_answers")),
        )
        if not answer:
            unanswered.append(question[:120] or "вопрос без подписи")
            continue
        target = yes if answer == "yes" else no
        if _click_option(page, str(target.get("id") or "")):
            filled.append(key)
        else:
            unanswered.append(question[:120] or "вопрос без подписи")

    return {
        "filled": list(dict.fromkeys(filled)),
        "unanswered": list(dict.fromkeys(unanswered)),
    }


def required_left(page) -> list[str]:
    """Что на форме ещё не заполнено из обязательного (глазами самой страницы)."""
    try:
        return page.evaluate(
            """() => {
                const left = [];
                const labelFor = (control) => {
                    const labelled = (control.getAttribute('aria-labelledby') || '')
                        .split(/\\s+/).filter(Boolean)
                        .map(x => (document.getElementById(x) || {}).innerText || '')
                        .join(' ').trim();
                    if (labelled) return labelled;
                    if (control.labels && control.labels.length) {
                        const text = [...control.labels]
                            .map(x => x.innerText || x.textContent || '').join(' ').trim();
                        if (text) return text;
                    }
                    const row = control.closest('.sapMInputBase, .sapMTextArea, .sapUiFormElement, tr');
                    const nearby = row && row.querySelector('label, .sapMLabel');
                    return ((nearby && (nearby.innerText || nearby.textContent))
                        || control.name || 'поле без подписи').trim();
                };
                document.querySelectorAll('input, textarea, select').forEach(control => {
                    if (control.type === 'file' || control.type === 'hidden' || control.disabled) return;
                    const labels = control.labels ? [...control.labels] : [];
                    const required = control.required
                        || control.getAttribute('aria-required') === 'true'
                        || !!control.closest('.sapMInputBaseRequired, .sapMTextAreaRequired')
                        || labels.some(x => x.classList.contains('sapMLabelRequired'));
                    const invalid = control.getAttribute('aria-invalid') === 'true'
                        || !!control.closest('.sapMInputBaseError, .sapMTextAreaError, .sapMInputBaseContentWrapperError');
                    if (!required && !invalid) return;
                    // sap.m.Select keeps an empty pseudo input for accessibility.
                    // Its value never changes; the real selected text lives in
                    // .sapMSltLabel. Only an empty visible label is unfinished.
                    if (control.classList.contains('sapUiPseudoInvisibleText')) {
                        const select = control.closest('.sapMSlt');
                        const visible = select && select.querySelector('.sapMSltLabel');
                        if (((visible && visible.textContent) || '').trim()) return;
                    }
                    if ((control.value || '').trim()) return;
                    left.push(labelFor(control).replace(/\\s*\\*\\s*$/, ''));
                });
                return [...new Set(left)].slice(0, 12);
            }"""
        ) or []
    except Exception:
        return []


_VALIDATION_ERROR_RE = re.compile(
    r"(Venligst\s+udfyld\s+alle\s+påkrævede\s+felter|"
    r"Please\s+fill\s+(?:in\s+)?all\s+required\s+fields|"
    r"Заполните\s+все\s+обязательные\s+поля)",
    re.I,
)


def post_submit_validation_errors(page) -> list[str]:
    """Read Lidl/UI5 validation shown only after the final button is clicked."""
    fields = required_left(page)
    try:
        text = page.locator(
            '[role="dialog"], .sapMMessageBox, .sapMDialog, .sapMMessageToast'
        ).all_inner_texts()
    except Exception:
        text = []
    if any(_VALIDATION_ERROR_RE.search(str(item or "")) for item in text):
        return fields or ["Lidl просит заполнить все обязательные поля"]
    return fields


_SUBMIT_TEXT_RE = re.compile(
    r"^\s*(Ansøg|Send ansøgning|Apply|Submit application|"
    r"Применять|Подать заявку|Отправить заявку)\s*$",
    re.I,
)
_RECEIPT_RE = re.compile(
    r"(tak\s+for\s+din\s+ansøgning|ansøgning(?:en)?\s+er\s+modtaget|"
    r"vi\s+har\s+modtaget\s+din\s+ansøgning|tak\s+for\s+din\s+interesse|"
    r"thank\s+you\s+for\s+your\s+application|application\s+(?:has\s+been\s+)?received|"
    r"спасибо\s+за\s+(?:вашу|твою)\s+заявку|заявк[ау]\s+(?:была\s+)?получен[ао])",
    re.I,
)
_CONFIRMATION_CONSENT_RE = re.compile(
    r"^\s*(Accepter|Accept|Accept all|"
    r"Принять|Принимать|Принять все|"
    r"Прийняти|Прийняти все)\s*$",
    re.I,
)


def _submit_button(page):
    """Return Lidl's real final button, never WexFlow's overlay control."""
    try:
        candidates = page.get_by_role("button", name=_SUBMIT_TEXT_RE)
        for index in range(candidates.count()):
            candidate = candidates.nth(index)
            if candidate.is_visible():
                return candidate
    except Exception:
        pass
    return None


def submission_checkpoint(page) -> dict:
    """Describe the non-destructive checkpoint at Lidl's final button."""
    button = _submit_button(page)
    reached = button is not None
    enabled = False
    if button is not None:
        try:
            enabled = bool(button.is_enabled())
        except Exception:
            enabled = False
    return {
        "reached_submit": reached,
        "submit_enabled": enabled,
        "submit_requested": bool(
            page.evaluate("() => Boolean(window.__wexflowSubmitRequested)")
        ) if reached else False,
    }


def submission_receipt_visible(page) -> bool:
    """Require positive Lidl confirmation text; a disappearing button is not enough."""
    try:
        text = page.locator("body").inner_text(timeout=1500)
    except Exception:
        return False
    return bool(_RECEIPT_RE.search(text or ""))


def _confirmation_consent_button(page):
    """Return Lidl's post-submit data/cookie consent button when it is visible."""
    try:
        candidates = page.get_by_role("button", name=_CONFIRMATION_CONSENT_RE)
        for index in range(candidates.count()):
            candidate = candidates.nth(index)
            if candidate.is_visible():
                return candidate
    except Exception:
        pass
    return None


def prepare_submission_proof(page, wait_seconds: float = 8.0) -> bool:
    """Clear Lidl's post-submit data dialog before taking the receipt proof.

    The action is deliberately limited to a page where Lidl's positive
    application receipt is already visible. It cannot accept a consent on an
    unsubmitted application or on an unrelated page.
    """
    if not submission_receipt_visible(page):
        return False
    button = _confirmation_consent_button(page)
    if button is None:
        return True
    print("  принимаю условия обработки данных на странице подтверждения Lidl")
    try:
        button.click(timeout=5000)
    except Exception as exc:
        print("  не удалось закрыть окно обработки данных Lidl:", str(exc)[:120])
        return False

    deadline = time.monotonic() + max(1.0, float(wait_seconds))
    while time.monotonic() < deadline:
        if not submission_receipt_visible(page):
            page.wait_for_timeout(250)
            continue
        if _confirmation_consent_button(page) is None:
            # Let the page finish its closing animation before the screenshot.
            page.wait_for_timeout(350)
            return True
        page.wait_for_timeout(250)
    return False


def take_explicit_submit_request(page) -> bool:
    """Atomically consume the review-card request for a trusted Playwright click."""
    try:
        return bool(page.evaluate(
            """() => {
                if (!window.__wexflowSubmitRequested) return false;
                window.__wexflowSubmitRequested = 0;
                return true;
            }"""
        ))
    except Exception:
        return False


def show_explicit_submit_result(page, state: str, message: str) -> None:
    """Show a browser-action result inside the review card, without hidden dialogs."""
    try:
        page.evaluate(
            """([state, message]) => {
                const host = document.getElementById('wexflow-banner');
                const root = host && host.shadowRoot;
                const action = root && root.getElementById('wexflow-real-submit');
                const note = root && root.getElementById('wexflow-submit-note');
                if (!action || !note) return;
                note.textContent = message || '';
                if (state === 'blocked') {
                    action.disabled = false;
                    action.textContent = 'Проверить и отправить снова';
                    action.style.background = '#f5c542';
                } else if (state === 'submitted') {
                    action.disabled = true;
                    action.textContent = 'Заявка отправлена';
                    action.style.background = '#16d86b';
                } else {
                    action.disabled = true;
                    action.textContent = 'Нужно проверить результат';
                    action.style.background = '#f5c542';
                }
            }""",
            [str(state or ""), str(message or "")],
        )
    except Exception:
        pass


def arm_explicit_submit(page) -> bool:
    """Add a review-card action consumed by the Playwright worker.

    A DOM ``button.click()`` is not trusted by SAP UI5 and can silently do
    nothing.  The card therefore emits a local signal; the worker consumes it
    and performs the native Lidl click through Playwright.
    """
    if _submit_button(page) is None:
        return False
    try:
        return bool(page.evaluate(
            """() => {
                const host = document.getElementById('wexflow-banner');
                const root = host && host.shadowRoot;
                if (!root) return false;
                if (root.getElementById('wexflow-real-submit')) return true;
                const action = document.createElement('button');
                action.id = 'wexflow-real-submit';
                action.type = 'button';
                action.textContent = 'Отправить до конца';
                action.style.cssText =
                    'display:block;width:100%;min-height:42px;padding:10px 12px;border:0;border-radius:9px;'
                    + 'background:#16d86b;color:#07170d;font:800 14px/1.2 Inter,Segoe UI,sans-serif;'
                    + 'white-space:normal;cursor:pointer;';
                const note = document.createElement('div');
                note.id = 'wexflow-submit-note';
                note.textContent =
                    'Проверит готовность и отправит заявку настоящей кнопкой Lidl.';
                note.style.cssText = 'margin-bottom:8px;color:#ffcf70;font-size:12px;line-height:1.35;';
                action.addEventListener('click', () => {
                    window.__wexflowSubmitRequested = Date.now();
                    action.disabled = true;
                    action.textContent = 'Проверяю и отправляю…';
                    note.textContent = 'Команда принята. WexFlow проверяет форму и нажимает кнопку Lidl…';
                });
                const actions = root.querySelector('.actions') || root.querySelector('.card');
                actions?.append(note, action);
                return true;
            }"""
        ))
    except Exception:
        return False


def set_submit_armed(page, checkpoint: dict, allow_submit: bool) -> None:
    """Expose the WexFlow submit signal only after an explicit caller opt-in."""
    if not checkpoint.get("reached_submit") or not allow_submit:
        checkpoint["submit_armed"] = False
        return
    checkpoint["submit_armed"] = arm_explicit_submit(page)


def prepare(page, url: str, profile: dict, allow_submit: bool = False) -> dict:
    print(f"  открываю Lidl EasyApply: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    page.get_by_label(re.compile(r"Fornavn", re.I)).first.wait_for(
        state="visible", timeout=60_000
    )
    dismiss_cookies(page)

    # Защита: сверяем форму с контрактом ДО первого ввода. Lidl переделал
    # анкету — ничего не заполняем и не жмём, человеку уходит понятный текст.
    contract = site_contract.check(page, "lidl_easy_apply")
    if not contract["ok"]:
        print("  форма Lidl изменилась:", contract.get("short") or "")
        raise site_contract.SiteChanged(contract)
    if contract["warnings"]:
        print("  предупреждение контракта:", ", ".join(contract["warnings"]))

    filled: list[str] = []
    fields = (
        ("Fornavn", profile.get("first_name"), "first_name"),
        ("Efternavn", profile.get("last_name"), "last_name"),
        ("E-mail-adresse", profile.get("email"), "email"),
        ("Mobilnummer", normalize_phone(profile.get("phone") or ""), "phone"),
    )
    for label, value, key in fields:
        if _fill_labeled(page, label, value or ""):
            filled.append(key)

    street, house_number = split_address(profile.get("address") or "")
    address_fields = (
        ("Gade", street, "street"),
        ("Husnummer", house_number, "house number"),
        ("Postnummer", profile.get("zip"), "zip"),
        ("By", profile.get("city"), "city"),
    )
    for caption, value, key in address_fields:
        if _fill_caption(page, caption, value or ""):
            filled.append(key)

    if _select_ui5(page, "Land", profile.get("country") or ""):
        filled.append("country")
    _wait_for_picker_to_close(page)
    cv_selector = 'input[type="file"][name="EACVUploader"]'
    cover_selector = 'input[type="file"][name="EACoverLetterUploader"]'
    uploaded_documents: dict[str, str] = {}
    cv_name = _upload(page, cv_selector, profile.get("cv_path") or "", "cv")
    if cv_name:
        uploaded_documents["cv"] = cv_name
        filled.append(f"CV: {cv_name}")
    cover_name = _upload(
        page, cover_selector, profile.get("cover_letter_path") or "", "cover"
    )
    if cover_name:
        uploaded_documents["cover"] = cover_name
        filled.append(f"письмо: {cover_name}")

    # Ответы для анкеты — из сохранённых ответов человека, ничего не выдумывая
    answers_report = fill_answers(page, profile)
    filled.extend(answers_report["filled"])

    questions = _screening_question_count(page)
    missing = list(answers_report["unanswered"])
    missing.extend(required_left(page))
    if not missing:
        missing = ["ничего — анкета заполнена полностью"]
    add_banner(
        page,
        questions,
        filled,
        platform="Lidl EasyApply",
        missing=missing,
    )
    checkpoint = submission_checkpoint(page)
    checkpoint["unanswered"] = answers_report["unanswered"]
    checkpoint["required_left"] = required_left(page)
    checkpoint["filled"] = filled
    checkpoint["documents"] = uploaded_documents
    set_submit_armed(page, checkpoint, allow_submit)
    print(f"  заполнено: {filled or '—'}")
    if checkpoint["reached_submit"]:
        print("  ДОШЁЛ ДО КНОПКИ ANSØG — подготовка её не нажимала.")
    else:
        print("  warning: финальная кнопка Ansøg не найдена.")
    if checkpoint["submit_armed"]:
        print("  РЕАЛЬНАЯ ОТПРАВКА ВКЛЮЧЕНА — только через отдельное подтверждение.")
    else:
        print("  ПРОВЕРКА БЕЗ ОТПРАВКИ — ответы, согласия и Ansøg оставлены тебе.")
    return checkpoint


def blockers(page, profile: dict) -> list[str]:
    """Почему автоматическая отправка сейчас невозможна. Пусто = можно жать.

    Список читает человек, поэтому пункты — человеческим языком.
    """
    reasons: list[str] = []
    answers = profile_store.answers(profile)
    for key in ("first_name", "last_name", "email", "phone"):
        if not str(profile.get(key) or "").strip():
            reasons.append(f"в профиле нет поля: {key}")
    for group in _radio_groups(page):
        options = list(group.get("options") or [])
        if any(option.get("checked") for option in options):
            continue
        question = str(group.get("question") or "").strip()
        yes = any(_YES_RE.match(str(o.get("text") or "")) for o in options)
        no = any(_NO_RE.match(str(o.get("text") or "")) for o in options)
        if not (yes and no):
            reasons.append("вопрос анкеты не «да/нет»: " + (question[:80] or "без подписи"))
            continue
        key, answer = question_answer(
            question,
            answers,
            allow_saved=bool(profile.get("_allow_shared_answers")),
        )
        if not answer:
            reasons.append("нет сохранённого ответа: " + (question[:80] or "вопрос без подписи"))
    reasons.extend("не заполнено обязательное поле: " + name for name in required_left(page))
    # Подстраховка от «не увидел вопрос»: Lidl показывает N вопросов вакансии,
    # а мы разобрали меньше — значит какой-то вопрос отрисован иначе. Жать
    # нельзя: непонятый вопрос опаснее неотправленной заявки.
    declared = _screening_question_count(page)
    seen = len(_radio_groups(page))
    if declared > seen:
        reasons.append(
            f"распознано {seen} вопросов из {declared} — остальные WexFlow не понял"
        )
    button = _submit_button(page)
    if button is None:
        reasons.append("кнопка Ansøg не найдена")
    else:
        try:
            if not button.is_enabled():
                reasons.append("Lidl держит кнопку Ansøg неактивной")
        except Exception:
            reasons.append("не удалось проверить кнопку Ansøg")
    # дубли не нужны: человеку важен список причин, а не их количество
    seen: set[str] = set()
    return [r for r in reasons if not (r in seen or seen.add(r))]


def submit(page, profile: dict, wait_seconds: float = 25.0) -> dict:
    """Реальная отправка заявки — только когда отвечать больше нечего.

    Возвращает {"state": submitted|blocked|no_receipt, "message": ...}.
    «Отправлено» пишем ТОЛЬКО по квитанции самого Lidl: исчезнувшая кнопка
    доказательством не считается.
    """
    left = blockers(page, profile)
    if left:
        return {"state": "blocked", "message": "; ".join(left[:6]), "blockers": left}
    button = _submit_button(page)
    print("  жму финальную кнопку Lidl — реальная отправка")
    button.click()
    deadline = time.monotonic() + max(5.0, float(wait_seconds))
    while time.monotonic() < deadline:
        if submission_receipt_visible(page):
            proof_ready = prepare_submission_proof(page)
            return {"state": "submitted",
                    "message": "Lidl показал квитанцию о получении заявки.",
                    "blockers": [],
                    "proof_ready": proof_ready}
        validation = post_submit_validation_errors(page)
        if validation:
            message = "Lidl не принял форму: " + "; ".join(validation[:6])
            return {"state": "blocked", "message": message, "blockers": validation}
        page.wait_for_timeout(500)
    return {
        "state": "no_receipt",
        "message": "Кнопка нажата, но Lidl не показал квитанцию. WexFlow проверит "
                   "личный кабинет; не подавай повторно до результата проверки.",
        "blockers": [],
    }
