"""Этап 3 — ассистированная подача заявки (режим «review then 1-click»).

Открывает ВИДИМОЕ окно браузера на странице подачи выбранной вакансии, использует
сохранённый логин (persistent context в browser_profile/), пытается best-effort
предзаполнить поля формы из profile.json и грузит CV, затем ОСТАНАВЛИВАЕТСЯ —
финальный просмотр и нажатие «Send/Submit» делает человек.

Запуск:  python apply.py <job_id>
         python apply.py --login        # один раз: войти/создать аккаунт кандидата

ВАЖНО: точные селекторы формы SuccessFactors зависят от конкретной вакансии и
становятся видны только после входа. Поэтому филлер — эвристический (по label/
placeholder/name) и сознательно не жмёт Submit. После первого реального прохода
поля можно «прибить гвоздями» под конкретную форму.
"""
import json
import re
import sys
import time
from pathlib import Path

# вывод может содержать датские/русские символы — заставляем UTF-8,
# иначе print падает с UnicodeEncodeError (cp1251) при перенаправлении в файл
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import config
from db import Job, get_session


def load_profile() -> dict:
    if not config.PROFILE_PATH.exists():
        sys.exit("Нет profile.json — скопируй profile.example.json в profile.json и заполни.")
    return json.loads(config.PROFILE_PATH.read_text(encoding="utf-8"))


def _mask_email(email: str) -> str:
    """ivan@gmail.com -> iv***@gmail.com (чтобы email не светился в логах)."""
    name, _, domain = (email or "").partition("@")
    if not domain:
        return "***"
    return f"{name[:2]}***@{domain}"


# карта: подстрока в label/name/placeholder -> ключ профиля
FIELD_HINTS = {
    "first": "first_name", "fornavn": "first_name", "given": "first_name",
    "last": "last_name", "efternavn": "last_name", "surname": "last_name",
    "email": "email", "e-mail": "email", "mail": "email",
    "phone": "phone", "telefon": "phone", "mobil": "phone",
    "address": "address", "adresse": "address", "street": "address",
    "zip": "zip", "postnr": "zip", "postal": "zip",
    "city": "city", "by": "city",
}


def add_job_banner(page, job: Job):
    if not job:
        return
    title = job.title or "вакансию"
    place = ", ".join(p for p in [job.city, job.street] if p)
    text = f"Ты подаёшься на: {title}" + (f" — {place}" if place else "")
    try:
        page.evaluate(
            """text => {
                const old = document.getElementById("saling-apply-banner");
                if (old) old.remove();
                const el = document.createElement("div");
                el.id = "saling-apply-banner";
                el.textContent = text;
                el.style.cssText = [
                    "position:fixed",
                    "left:16px",
                    "right:16px",
                    "top:12px",
                    "z-index:2147483647",
                    "background:#0f172a",
                    "color:#fff",
                    "border:1px solid #2563eb",
                    "box-shadow:0 12px 35px rgba(0,0,0,.35)",
                    "border-radius:10px",
                    "padding:12px 16px",
                    "font:600 15px system-ui,Segoe UI,sans-serif",
                    "pointer-events:none"
                ].join(";");
                document.body.appendChild(el);
            }""",
            text,
        )
    except Exception:
        pass


def _first_visible(locator):
    try:
        count = locator.count()
        for i in range(count):
            item = locator.nth(i)
            if item.is_visible():
                return item
    except Exception:
        pass
    return None


_NEXT_RE = re.compile(r"næste|fortsæt|videre|continue|next|log på|log ind|sign in|log in|login", re.I)
_SIGNIN_RE = re.compile(r"sign in|log ?in|login|log på|log ind|submit|send|fortsæt", re.I)


def _login_present(page) -> bool:
    """Видна ли форма входа. Учитывает одношаговый (есть поле пароля) и
    двухшаговый (email-first: сначала email + кнопка «Далее/Log på») вход.
    Если уже на форме заявки (есть file-input) — считаем, что вошли."""
    try:
        for el in page.query_selector_all('input[type="password"]'):
            if el.is_visible():
                return True
        # уже на форме подачи?
        for fr in _all_frames(page):
            try:
                if fr.query_selector('input[type="file"]'):
                    return False
            except Exception:
                continue
        # email-first шаг: видимое email-поле + кнопка входа/далее
        email = _first_visible(page.locator('input[type="email"], input[name*="mail" i], input[id*="mail" i], input[id*="user" i]'))
        if email:
            btn = _first_visible(page.get_by_role("button", name=_NEXT_RE))
            if btn:
                return True
        return False
    except Exception:
        return False


def try_login(page, profile: dict) -> bool:
    """Вход с сохранёнными данными. Поддерживает двухшаговый вход
    (email → «Далее» → пароль → «Войти»)."""
    if not _login_present(page):
        return True

    creds = {}
    try:
        import credentials_store
        creds = credentials_store.get()
    except Exception:
        creds = {}
    username = creds.get("email") or profile.get("email") or ""
    password = creds.get("password") or ""
    if not username and not password:
        print("  логин: сохранённых данных нет — войди вручную в окне.")
        return not _login_present(page)

    try:
        # 1) email
        email_input = _first_visible(page.locator('input[type="email"], input[name*="mail" i], input[id*="mail" i], input[id*="user" i], input[type="text"]'))
        if email_input and username and not email_input.input_value():
            email_input.fill(username)
            print(f"  логин: ввёл email {_mask_email(username)}")
        # 2) если пароля ещё нет (двухшаговый) — жмём «Далее»
        password_input = _first_visible(page.locator('input[type="password"]'))
        if not password_input:
            nextbtn = _first_visible(page.get_by_role("button", name=_NEXT_RE)) or _first_visible(page.locator('button[type="submit"], input[type="submit"]'))
            if nextbtn:
                nextbtn.click()
                print("  логин: нажал «Далее» (двухшаговый вход)")
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(2000)
                password_input = _first_visible(page.locator('input[type="password"]'))
        # 3) пароль
        if password_input and password and not password_input.input_value():
            password_input.fill(password)
            print("  логин: ввёл пароль")
        # 4) кнопка входа
        if password_input and password_input.input_value():
            button = _first_visible(page.get_by_role("button", name=_SIGNIN_RE)) or _first_visible(page.locator('button[type="submit"], input[type="submit"], button'))
            if button:
                button.click()
                print("  логин: нажал «Войти»")
                try:
                    page.wait_for_load_state("networkidle", timeout=30000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(2500)
    except Exception as e:
        print("  логин warn:", str(e)[:80])
    ok = not _login_present(page)
    print("  логин:", "успешно ✔" if ok else "не удалось — войди вручную в окне (бот продолжит)")
    return ok


def _login_banner(page):
    """Подсказка в окне браузера: войти вручную (для Google/2-шаговых входов)."""
    try:
        page.evaluate("""() => {
            const old = document.getElementById("saling-login-banner");
            if (old) old.remove();
            const el = document.createElement("div");
            el.id = "saling-login-banner";
            el.textContent = "⬆ Войди в аккаунт Salling в этом окне — бот САМ продолжит после входа (откроет форму и прикрепит письмо).";
            el.style.cssText = ["position:fixed","left:16px","right:16px","bottom:16px",
                "z-index:2147483647","background:#0f172a","color:#fff","border:2px solid #22c55e",
                "box-shadow:0 12px 35px rgba(0,0,0,.4)","border-radius:10px","padding:14px 18px",
                "font:600 15px system-ui,Segoe UI,sans-serif","text-align:center"].join(";");
            document.body.appendChild(el);
        }""")
    except Exception:
        pass


def wait_for_login_if_needed(page, profile: dict, max_seconds: int = 240):
    if try_login(page, profile):
        return
    # автологин не прошёл (часто вход через Google/2 шага) — ждём ручного входа
    print("  Нужен вход. Жду, пока залогинишься вручную в окне браузера…")
    deadline = time.time() + max_seconds
    while time.time() < deadline and _login_present(page):
        _login_banner(page)
        page.wait_for_timeout(1500)
        try_login(page, profile)
    if not _login_present(page):
        print("  Вход выполнен — продолжаю.")
        try:
            page.evaluate("() => { const e=document.getElementById('saling-login-banner'); if(e) e.remove(); }")
        except Exception:
            pass


def click_apply_if_needed(page, job: Job):
    """После логина SuccessFactors иногда показывает список с кнопкой Apply.
    Нажимаем кнопку, чтобы открыть модальное окно заявки."""
    try:
        if page.locator('input[type="file"]').count() > 0 or page.get_by_text("Attach coverletter", exact=False).count() > 0:
            return
    except Exception:
        pass

    patterns = []
    if job.requisition_id:
        patterns.append(str(job.requisition_id))
    if job.title:
        patterns.append(job.title[:40])

    try:
        for pattern in patterns:
            row = page.locator("tr, li, div").filter(has_text=pattern).first
            if row.count() and row.is_visible():
                btn = _first_visible(row.get_by_role("button", name=re.compile(r"apply", re.I)))
                if btn:
                    btn.click()
                    page.wait_for_timeout(2500)
                    return
        btn = _first_visible(page.get_by_role("button", name=re.compile(r"^apply$", re.I)))
        if btn:
            btn.click()
            page.wait_for_timeout(2500)
    except Exception:
        pass


def _form_present(page) -> bool:
    """Форма заявки видна (в любом фрейме): есть file-input или поле/кнопка вложения."""
    for fr in _all_frames(page):
        try:
            if fr.query_selector('input[type="file"]'):
                return True
            if fr.get_by_text(re.compile(r"vedhæft ansøgning|attach coverletter|vælg fil|tilføj cv", re.I)).count() > 0:
                return True
        except Exception:
            continue
    return False


def wait_for_application_form(page, job: Job):
    click_apply_if_needed(page, job)
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            if _form_present(page):
                return
            click_apply_if_needed(page, job)
        except Exception:
            pass
        page.wait_for_timeout(1000)


def _context_text(el) -> str:
    try:
        return el.evaluate(
            """node => {
                let cur = node;
                let chunks = [];
                for (let i = 0; cur && i < 5; i++, cur = cur.parentElement) {
                    chunks.push(cur.innerText || cur.textContent || "");
                }
                return chunks.join(" ").toLowerCase();
            }"""
        )
    except Exception:
        return ""


def _set_file(el, path: str, label: str) -> bool:
    if not path or not Path(path).exists():
        return False
    try:
        el.set_input_files(path)
        print(f"  {label} загружен: {path}")
        return True
    except Exception:
        return False


def _all_frames(page):
    """Главный фрейм + все вложенные (форма Salling часто внутри iframe)."""
    try:
        return list(page.frames)
    except Exception:
        return [page]


def clear_autofilled_emails(page):
    """Chrome порой вписывает email в текстовые поля формы Salling — вычищаем их
    во всех фреймах, чтобы в «Vedhæft ansøgning» не оставалось чужой почты."""
    cleared = 0
    for fr in _all_frames(page):
        try:
            for el in fr.query_selector_all('input[type="text"], input[type="email"], input:not([type])'):
                try:
                    if not el.is_visible() or not el.is_editable():
                        continue
                    v = (el.input_value() or "").strip()
                    if v and "@" in v and "." in v and " " not in v:
                        el.fill("")
                        cleared += 1
                except Exception:
                    continue
        except Exception:
            continue
    if cleared:
        print(f"  очищено автозаполненных email-полей: {cleared}")


def _click_upload_by_text(page, frame, text_re, path, label) -> bool:
    """Находит ВИДИМЫЙ кликабельный элемент с подходящим ТЕКСТОМ (в любом теге) и
    через перехват file chooser отдаёт файл. Для Salling: 'Vælg fil' = письмо,
    'Tilføj CV' = CV, 'Tilføj' = документы."""
    if not path or not Path(path).exists():
        return False
    # сначала настоящие кнопки/ссылки, потом любые элементы с текстом
    selectors = ["button", "[role=button]", "a", "span", "div", "bdi"]
    seen = set()
    for sel in selectors:
        try:
            els = frame.query_selector_all(sel)
        except Exception:
            continue
        for el in els:
            try:
                if not el.is_visible():
                    continue
                txt = (el.inner_text() or "").strip()
            except Exception:
                continue
            if not txt or len(txt) > 40 or not text_re.search(txt):
                continue
            key = txt.lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                with page.expect_file_chooser(timeout=7000) as fc:
                    el.click()
                fc.value.set_files(path)
                print(f"  {label} прикреплён (клик по '{txt[:30]}'): {Path(path).name}")
                return True
            except Exception as e:
                print(f"  {label}: клик по '{txt[:30]}' не открыл выбор файла ({str(e)[:60]})")
    return False


def _dump_form(frames):
    """Подробный дамп формы в лог — чтобы видеть реальную структуру."""
    print(f"  [форма] фреймов: {len(frames)}")
    for idx, fr in enumerate(frames):
        try:
            fis = fr.query_selector_all('input[type="file"]')
        except Exception:
            fis = []
        clickable_txt = []
        try:
            for el in fr.query_selector_all("button, [role=button], a, .sapMBtn"):
                try:
                    if not el.is_visible():
                        continue
                    t = (el.inner_text() or "").strip()
                except Exception:
                    continue
                if t and len(t) < 40 and re.search(r"vælg|tilføj|vedhæft|attach|upload|browse|ansøg|send|gem", t, re.I):
                    clickable_txt.append(t)
        except Exception:
            pass
        if fis or clickable_txt:
            print(f"    фрейм #{idx} url={(fr.url or '')[:70]}")
            print(f"      file-input: {len(fis)}")
            for fi in fis:
                try:
                    info = fi.evaluate("e=>({id:e.id,name:e.name,accept:e.accept})")
                    print(f"        file: {info}")
                except Exception:
                    pass
            if clickable_txt:
                print(f"      кнопки: {clickable_txt[:12]}")


def upload_documents(page, profile: dict):
    cv = profile.get("cv_path")
    cover = profile.get("cover_letter_path")

    # убрать подсказку про логин, если осталась
    try:
        page.evaluate("() => { const e=document.getElementById('saling-login-banner'); if(e) e.remove(); }")
    except Exception:
        pass

    clear_autofilled_emails(page)

    cover_re = re.compile(r"ansøg|cover|motivation|følgebrev", re.I)
    cv_re = re.compile(r"\bcv\b|resume|curriculum", re.I)
    # текст КНОПОК (а не контекста)
    cover_btn_re = re.compile(r"vælg fil|vedhæft ansøg|vælg|browse|upload|attach", re.I)
    cv_btn_re = re.compile(r"tilføj cv|vælg cv|upload cv", re.I)

    frames = _all_frames(page)
    _dump_form(frames)

    cover_uploaded = False
    cv_uploaded = False

    for fr in frames:
        # 1) Письмо — клик по кнопке «Vælg fil»
        if cover and not cover_uploaded:
            cover_uploaded = _click_upload_by_text(page, fr, cover_btn_re, cover, "Мотивационное письмо")
            if not cover_uploaded:  # фолбэк: скрытое file-поле по контексту
                try:
                    for fi in fr.query_selector_all('input[type="file"]'):
                        if cover_re.search(_context_text(fi)):
                            if _set_file(fi, cover, "Мотивационное письмо"):
                                cover_uploaded = True
                                break
                except Exception:
                    pass
        # 2) CV — клик по «Tilføj CV»
        if cv and not cv_uploaded:
            cv_uploaded = _click_upload_by_text(page, fr, cv_btn_re, cv, "CV")
            if not cv_uploaded:
                try:
                    for fi in fr.query_selector_all('input[type="file"]'):
                        if cv_re.search(_context_text(fi)):
                            if _set_file(fi, cv, "CV"):
                                cv_uploaded = True
                                break
                except Exception:
                    pass

    # последний шанс для письма: любое ещё не использованное pdf-поле
    if cover and not cover_uploaded:
        for fr in frames:
            try:
                for fi in fr.query_selector_all('input[type="file"]'):
                    acc = (fi.get_attribute("accept") or "").lower()
                    if "pdf" in acc or acc == "":
                        if _set_file(fi, cover, "Мотивационное письмо (запасной вариант)"):
                            cover_uploaded = True
                            break
            except Exception:
                pass
            if cover_uploaded:
                break

    clear_autofilled_emails(page)

    if cover and not cover_uploaded:
        print("  Мотивационное письмо НЕ прикрепилось — см. дамп формы выше.")
    if cv and not cv_uploaded:
        print("  CV не прикрепил (возможно, сайт уже помнит резюме).")


def best_effort_fill(page, profile: dict):
    filled = 0
    inputs = page.query_selector_all("input, textarea")
    for el in inputs:
        try:
            if not el.is_visible() or not el.is_editable():
                continue
            t = (el.get_attribute("type") or "").lower()
            if t in ("hidden", "password", "submit", "button", "checkbox", "radio", "file"):
                continue
            label = " ".join(filter(None, [
                el.get_attribute("name"), el.get_attribute("id"),
                el.get_attribute("placeholder"), el.get_attribute("aria-label"),
            ])).lower()
            for hint, key in FIELD_HINTS.items():
                if hint in label and profile.get(key) and not el.input_value():
                    el.fill(str(profile[key]))
                    filled += 1
                    break
        except Exception:
            continue
    upload_documents(page, profile)
    print(f"  Предзаполнено полей: {filled}")


def _wait_until_browser_closed(ctx):
    while True:
        try:
            pages = [p for p in ctx.pages if not p.is_closed()]
            if not pages:
                return
            time.sleep(1)
        except KeyboardInterrupt:
            return
        except Exception:
            return


def _disable_autofill(user_data_dir: str):
    """Отключает автозаполнение и менеджер паролей в профиле браузера бота,
    чтобы Chrome не подставлял чужой email в поля формы Salling."""
    try:
        default = Path(user_data_dir) / "Default"
        default.mkdir(parents=True, exist_ok=True)
        pref = default / "Preferences"
        data = {}
        if pref.exists():
            try:
                data = json.loads(pref.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data.setdefault("autofill", {})
        data["autofill"]["profile_enabled"] = False
        data["autofill"]["credit_card_enabled"] = False
        data["credentials_enable_service"] = False
        data.setdefault("profile", {})
        data["profile"]["password_manager_enabled"] = False
        pref.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _launch_browser(p):
    """Запускает ВИДИМЫЙ браузер с сохранённым профилем.

    Сначала пробует системный Chrome/Edge (channel), потому что встроенный в
    Playwright Chromium на этой машине не стартует из-за отсутствующего
    Visual C++ Redistributable («side-by-side configuration is incorrect»).
    Системные браузеры имеют все зависимости и открываются без проблем.
    """
    _disable_autofill(str(config.BROWSER_PROFILE_DIR))
    no_autofill_args = [
        "--disable-features=AutofillServerCommunication,AutofillEnableAccountWalletStorage,PasswordManagerOnboarding",
        "--disable-save-password-bubble",
    ]
    attempts = [
        {"channel": "chrome"},
        {"channel": "msedge"},
        {},  # встроенный Chromium как последний шанс
    ]
    last_err = None
    for opts in attempts:
        try:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(config.BROWSER_PROFILE_DIR),
                headless=False,
                locale="da-DK",
                args=no_autofill_args,
                **opts,
            )
            label = opts.get("channel", "bundled chromium")
            print(f"  браузер: {label}")
            return ctx
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(
        "Не удалось открыть видимый браузер. Установи Google Chrome или "
        "Microsoft Visual C++ Redistributable (x64). Последняя ошибка: "
        f"{last_err}"
    )


def accept_consent(page) -> bool:
    """Подтверждает всплывающие согласия (Accepter/Godkend/OK), НЕ нажимая Afvis.
    На Salling при отправке вылезает «Samtykke til behandling af personoplysninger»."""
    rx = re.compile(r"^\s*(accepter|acceptér|accept|godkend|tillad|ja,? tak|bekræft|ok)\s*$", re.I)
    clicked = False
    for fr in _all_frames(page):
        try:
            els = fr.query_selector_all(
                "button, [role=button], a, .sapMBtn, bdi, input[type=button], input[type=submit]")
        except Exception:
            continue
        for el in els:
            try:
                if not el.is_visible():
                    continue
                t = (el.inner_text() or el.get_attribute("value") or "").strip()
            except Exception:
                continue
            if t and rx.match(t) and "afvis" not in t.lower():
                try:
                    el.click()
                    print(f"  Подтвердил согласие: '{t[:20]}'")
                    page.wait_for_timeout(1500)
                    clicked = True
                except Exception:
                    pass
    return clicked


def _ansog_present(page) -> bool:
    rx = re.compile(r"^\s*(ansøg|send ansøgning|indsend)\s*$", re.I)
    for fr in _all_frames(page):
        try:
            for el in fr.query_selector_all("button, [role=button], a, .sapMBtn, bdi"):
                try:
                    if el.is_visible() and rx.match((el.inner_text() or "").strip()):
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _submission_confirmed(page) -> bool:
    """Признаки, что заявка реально ушла (страница благодарности/квитанция)."""
    rx = re.compile(
        r"tak for din ansøgning|din ansøgning er (modtaget|sendt|registreret)|"
        r"modtaget din ansøgning|kvittering|tak,? fordi du (søgte|ansøgte)|"
        r"thank you for your application|application (received|submitted)|ansøgning sendt",
        re.I)
    for fr in _all_frames(page):
        try:
            if fr.get_by_text(rx).count() > 0:
                return True
        except Exception:
            continue
    return False


def submit_application(page) -> bool:
    """Жмёт «Ansøg», подтверждает согласие и проверяет, что заявка реально ушла.
    Возвращает True ТОЛЬКО если отправка подтвердилась."""
    accept_consent(page)  # вдруг согласие висит ещё до отправки
    rx = re.compile(r"^\s*(ansøg|send ansøgning|send|indsend)\s*$", re.I)
    clicked = False
    for fr in _all_frames(page):
        if clicked:
            break
        try:
            els = fr.query_selector_all("button, [role=button], a, .sapMBtn, bdi")
        except Exception:
            continue
        for el in els:
            try:
                if not el.is_visible():
                    continue
                t = (el.inner_text() or "").strip()
            except Exception:
                continue
            if t and rx.match(t) and "annul" not in t.lower():
                el.click()
                print(f"  Нажал отправку: '{t[:20]}'")
                page.wait_for_timeout(2500)
                clicked = True
                break
    if not clicked:
        print("  Кнопку отправки (Ansøg) не нашёл — проверь вручную.")
        return False
    # после Ansøg вылезает согласие на обработку данных — подтверждаем (до 3 раз)
    for _ in range(3):
        if accept_consent(page):
            page.wait_for_timeout(1500)
        else:
            break
    page.wait_for_timeout(2500)
    # иногда после согласия нужно ещё раз нажать Ansøg
    if _ansog_present(page):
        for fr in _all_frames(page):
            try:
                for el in fr.query_selector_all("button, [role=button], a, .sapMBtn, bdi"):
                    try:
                        t = (el.inner_text() or "").strip()
                    except Exception:
                        continue
                    if t and rx.match(t) and "annul" not in t.lower():
                        el.click(); page.wait_for_timeout(2000)
                        break
            except Exception:
                continue
        for _ in range(3):
            if accept_consent(page):
                page.wait_for_timeout(1500)
            else:
                break
        page.wait_for_timeout(2000)
    # успех = есть подтверждение ИЛИ форма закрылась (кнопки Ansøg больше нет)
    ok = _submission_confirmed(page) or not _ansog_present(page)
    print("  ✔ отправка подтверждена" if ok else "  ⚠ отправка НЕ подтвердилась — проверь вручную")
    return ok


def _save_proof(page, job):
    """Сохраняет скриншот результата подачи в logs/applied/ как доказательство."""
    try:
        from datetime import datetime
        out = config.BASE_DIR / "logs" / "applied"
        out.mkdir(parents=True, exist_ok=True)
        rid = job.requisition_id or job.id
        name = f"{datetime.now():%Y%m%d_%H%M%S}_{rid}.png"
        page.screenshot(path=str(out / name), full_page=True)
        print(f"  📸 скрин-пруф: logs/applied/{name}")
    except Exception as e:
        print("  не смог сохранить скрин:", e)


def _mark_applied(job_id: str):
    """Отмечает вакансию как поданную в дашборде после реальной отправки."""
    try:
        from db import utcnow
        with get_session() as s:
            j = s.get(Job, job_id)
            if j:
                j.status = "applied"
                j.applied_at = utcnow()
                s.add(j)
                s.commit()
        print("  ✔ отмечено «подано» в дашборде")
    except Exception as e:
        print("  не смог отметить applied:", e)


def process_job(page, job, profile, submit: bool):
    """Открывает вакансию, ждёт логин, открывает форму, грузит файлы, опц. отправляет."""
    print(f"\n=== {job.title} — {job.city} ===\n{job.application_link}")
    try:
        page.goto(job.application_link, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print("  goto warning:", e)
    page.wait_for_timeout(2500)
    try:
        wait_for_login_if_needed(page, profile)
        add_job_banner(page, job)
        wait_for_application_form(page, job)
        accept_consent(page)
        add_job_banner(page, job)
        upload_documents(page, profile)
    except Exception as e:
        print("  warning:", e)
    if submit:
        ok = submit_application(page)
        if ok:
            print("  ОТПРАВЛЕНО ✔")
            _save_proof(page, job)
            _mark_applied(job.id)
        else:
            print("  отправка не нажалась — проверь вручную")
    else:
        print("  Прогон без отправки — проверь форму и нажми Ansøg сам.")


def run(job_id: str | None, login_only: bool = False, web_mode: bool = False, submit: bool = False):
    profile = load_profile()
    config.BROWSER_PROFILE_DIR.mkdir(exist_ok=True)

    job = None
    if not login_only:
        with get_session() as s:
            job = s.get(Job, job_id)
        if not job:
            sys.exit(f"Вакансия {job_id} не найдена в БД.")

    with sync_playwright() as p:
        ctx = _launch_browser(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if login_only:
            try:
                page.goto("https://candidatecareercockpit-a3r1eyssyw.dispatcher.hana.ondemand.com/",
                          wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print("  goto warning:", e)
            print("\n>>> Войди/создай аккаунт кандидата вручную. Сессия сохранится в browser_profile/.")
        else:
            process_job(page, job, profile, submit)

        if web_mode:
            print("\n>>> Браузер останется открытым. Закрой его, когда закончишь.")
            _wait_until_browser_closed(ctx)
        else:
            input("\nНажми Enter здесь, когда закончишь, чтобы закрыть браузер...")
        ctx.close()


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def run_batch(job_ids, submit: bool = False, web_mode: bool = True, concurrency: int = 2):
    """Пакетная подача: открывает по `concurrency` вкладок одновременно (загрузка
    идёт параллельно), обрабатывает, затем следующую пачку. Один браузер = один логин."""
    profile = load_profile()
    config.BROWSER_PROFILE_DIR.mkdir(exist_ok=True)
    jobs = []
    with get_session() as s:
        for jid in job_ids:
            j = s.get(Job, jid)
            if j:
                jobs.append(j)
    if not jobs:
        sys.exit("Не нашёл выбранных вакансий в БД.")
    print(f"Пакетная подача: вакансий {len(jobs)}, по {concurrency} за раз, "
          f"{'С ОТПРАВКОЙ' if submit else 'прогон без отправки'}")

    submitted = 0
    with sync_playwright() as p:
        ctx = _launch_browser(p)
        # закрыть стартовую пустую вкладку позже; пока используем новые
        for batch in _chunks(jobs, concurrency):
            pages = []
            # 1) открыть все вкладки пачки и запустить загрузку параллельно
            for job in batch:
                pg = ctx.new_page()
                try:
                    pg.goto(job.application_link, wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    print("  goto warning:", e)
                pages.append((pg, job))
            # 2) обработать каждую вкладку пачки
            for pg, job in pages:
                pg.bring_to_front()
                print(f"\n=== {job.title} — {job.city} ===")
                pg.wait_for_timeout(1500)
                try:
                    wait_for_login_if_needed(pg, profile)
                    add_job_banner(pg, job)
                    wait_for_application_form(pg, job)
                    accept_consent(pg)
                    add_job_banner(pg, job)
                    upload_documents(pg, profile)
                    if submit:
                        ok = submit_application(pg)
                        if ok:
                            print("  ОТПРАВЛЕНО ✔")
                            _save_proof(pg, job)
                            _mark_applied(job.id)
                            submitted += 1
                        else:
                            print("  ⚠ НЕ отправилось (не отмечаю как поданное)")
                    else:
                        print("  прогон: не отправляю, не отмечаю")
                except Exception as e:
                    print("  warning:", e)
            # 3) при авто-отправке закрываем вкладки пачки и идём дальше;
            #    в режиме прогона оставляем открытыми для проверки
            if submit:
                for pg, _ in pages:
                    try:
                        pg.close()
                    except Exception:
                        pass

        if submit:
            print(f"\n========\nИТОГ: реально отправлено и отмечено «подано»: {submitted} из {len(jobs)}")
            if submitted < len(jobs):
                print("Остальные не подтвердили отправку — проверь их вручную.")
        else:
            print(f"\n========\nПрогон завершён ({len(jobs)} вакансий открыто) — НЕ отправлял, ничего не отмечал.")

        if web_mode:
            print("\n>>> Готово. Браузер остаётся открытым — проверь/закрой сам.")
            _wait_until_browser_closed(ctx)
        ctx.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit("Использование: python apply.py <job_id> [<job_id> ...] [--submit] [--web] | python apply.py --login")
    web_mode = "--web" in args
    submit = "--submit" in args
    ids = [a for a in args if not a.startswith("--")]
    if "--login" in args:
        run(None, login_only=True, web_mode=web_mode)
    elif len(ids) > 1:
        run_batch(ids, submit=submit, web_mode=web_mode)
    else:
        run(ids[0], web_mode=web_mode, submit=submit)
