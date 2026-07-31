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


def _fill_labeled(page, label: str, value: str) -> bool:
    value = str(value or "").strip()
    if not value:
        return False
    try:
        control = page.get_by_label(re.compile(re.escape(label), re.I)).first
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


def _upload(page, selector: str, path: str, role: str = "document") -> bool:
    path = str(path or "").strip()
    if not path or not Path(path).is_file():
        return False
    try:
        path = profile_store.safe_document_upload_path(path, role)
        control = page.locator(selector).first
        if control.count():
            control.set_input_files(path)
            return True
    except Exception:
        pass
    return False


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
_QUESTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("work_night", re.compile(r"\bnat(?:tevagt|arbejde|hold)?\b", re.I)),
    ("work_early", re.compile(r"\b0[3-7][.:]\d{2}\b|tidlig|morgen", re.I)),
    ("work_evenings", re.compile(r"\baften\b|\b(?:19|20|21|22)[.:]\d{2}\b", re.I)),
    ("work_weekends", re.compile(r"weekend|lørdag|søndag", re.I)),
    ("has_drivers_license", re.compile(r"kørekort|driving licen[cs]e", re.I)),
    ("retail_experience", re.compile(r"erfaring.*(?:detail|butik|retail)|"
                                     r"(?:detail|butik|retail).*erfaring", re.I)),
)

_GENDER_LABELS = {
    "male": ("Mand", "Male", "Mænd"),
    "female": ("Kvinde", "Female", "Kvinder"),
    "other": ("Andet", "Other", "Ønsker ikke at oplyse"),
}

_YES_RE = re.compile(r"^\s*(ja|yes)\s*$", re.I)
_NO_RE = re.compile(r"^\s*(nej|no)\s*$", re.I)


def question_answer(text: str, answers: dict) -> tuple[str, str]:
    """(ключ ответа, «yes»/«no»/'') для текста вопроса анкеты."""
    clean = str(text or "")
    for key, rx in _QUESTION_RULES:
        if rx.search(clean):
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


def fill_answers(page, profile: dict) -> dict:
    """Ответить на вопросы Lidl сохранёнными ответами человека.

    Возвращает отчёт: что заполнено и какие вопросы остались без ответа
    (по ним автоподача не пойдёт).
    """
    answers = profile_store.answers(profile)
    filled: list[str] = []
    unanswered: list[str] = []

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
        for label in ("startdato", "Startdato", "start"):
            if _fill_labeled(page, label, human_date):
                filled.append("дата выхода")
                break

    for group in _radio_groups(page):
        question = str(group.get("question") or "").strip()
        options = list(group.get("options") or [])
        if any(option.get("checked") for option in options):
            continue
        yes = next((o for o in options if _YES_RE.match(str(o.get("text") or ""))), None)
        no = next((o for o in options if _NO_RE.match(str(o.get("text") or ""))), None)
        if not yes or not no:
            continue                      # не «да/нет» — не наш случай, не трогаем
        key, answer = question_answer(question, answers)
        if not answer:
            unanswered.append(question[:120] or "вопрос без подписи")
            continue
        target = yes if answer == "yes" else no
        if _click_option(page, str(target.get("id") or "")):
            filled.append(key)
        else:
            unanswered.append(question[:120] or "вопрос без подписи")

    return {"filled": filled, "unanswered": unanswered}


def required_left(page) -> list[str]:
    """Что на форме ещё не заполнено из обязательного (глазами самой страницы)."""
    try:
        return page.evaluate(
            """() => {
                const left = [];
                document.querySelectorAll('input[aria-required="true"], .sapMInputBaseRequired input')
                    .forEach(input => {
                        if (input.type === 'file' || input.disabled) return;
                        if ((input.value || '').trim()) return;
                        const id = input.getAttribute('aria-labelledby') || '';
                        const label = id.split(/\\s+/)
                            .map(x => (document.getElementById(x) || {}).innerText || '')
                            .join(' ').trim();
                        left.push(label || input.name || 'поле без подписи');
                    });
                return left.slice(0, 12);
            }"""
        ) or []
    except Exception:
        return []


_SUBMIT_TEXT_RE = re.compile(r"^\s*(Ansøg|Send ansøgning)\s*$", re.I)
_RECEIPT_RE = re.compile(
    r"(tak\s+for\s+din\s+ansøgning|ansøgning(?:en)?\s+er\s+modtaget|"
    r"vi\s+har\s+modtaget\s+din\s+ansøgning|tak\s+for\s+din\s+interesse)",
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


def arm_explicit_submit(page) -> bool:
    """Add a separate WexFlow final action that clicks Lidl only after confirmation."""
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
                action.textContent = 'Отправить заполненную анкету';
                action.style.cssText =
                    'width:100%;margin-top:10px;padding:10px 12px;border:0;border-radius:9px;'
                    + 'background:#16d86b;color:#07170d;font-weight:800;cursor:pointer;';
                const note = document.createElement('div');
                note.textContent =
                    'Это реальная отправка. Сначала заполни оставшиеся вопросы Lidl.';
                note.style.cssText = 'margin-top:8px;color:#ffcf70;font-size:12px;';
                action.addEventListener('click', () => {
                    const buttons = [...document.querySelectorAll('button')];
                    const nativeButton = buttons.find(button =>
                        /^(Ansøg|Send ansøgning)$/i.test((button.innerText || '').trim()));
                    if (!nativeButton || nativeButton.disabled
                            || nativeButton.getAttribute('aria-disabled') === 'true') {
                        alert('Форма Lidl ещё не готова: заполни обязательные поля и вопросы.');
                        return;
                    }
                    if (!confirm(
                        'Отправить эту заявку в Lidl сейчас? После подтверждения отменить нельзя.'
                    )) return;
                    window.__wexflowSubmitRequested = Date.now();
                    nativeButton.click();
                    action.disabled = true;
                    action.textContent = 'Отправляю…';
                });
                root.querySelector('.card')?.append(note, action);
                return true;
            }"""
        ))
    except Exception:
        return False


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
    if _upload(page, 'input[type="file"][name="EACVUploader"]',
               profile.get("cv_path") or "", "cv"):
        filled.append("CV")
    if _upload(page, 'input[type="file"][name="EACoverLetterUploader"]',
               profile.get("cover_letter_path") or "", "cover"):
        filled.append("cover letter")

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
    if allow_submit and checkpoint["reached_submit"]:
        checkpoint["submit_armed"] = arm_explicit_submit(page)
    else:
        checkpoint["submit_armed"] = False
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
        key, answer = question_answer(question, answers)
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
    try:
        page.evaluate("() => { window.__wexflowSubmitRequested = Date.now(); }")
    except Exception:
        pass
    print("  жму Ansøg — реальная отправка")
    button.click()
    deadline = time.monotonic() + max(5.0, float(wait_seconds))
    while time.monotonic() < deadline:
        if submission_receipt_visible(page):
            return {"state": "submitted",
                    "message": "Lidl показал квитанцию о получении заявки.",
                    "blockers": []}
        page.wait_for_timeout(500)
    return {
        "state": "no_receipt",
        "message": "Кнопка нажата, но Lidl не показал квитанцию — проверь почту "
                   "и личный кабинет, прежде чем подавать снова.",
        "blockers": [],
    }
