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
import io
import json
import re
import subprocess
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
import profile_store
import document_rules
from connectors import site_contract
from db import Job, get_session, init_db


def load_profile() -> dict:
    """Профиль кандидата берём из ОБЩЕГО хранилища WexFlow (profile_store) —
    того же места, куда его сохраняет приложение.

    Раньше читали config.PROFILE_PATH (старый модульный путь). В собранном
    приложении общий и модульный пути не совпадают: приложение писало профиль
    в общий файл, а подача искала его в модульном, не находила и молча падала
    здесь (sys.exit) ещё до открытия браузера — со стороны это выглядело как
    «отправка пошла, но ничего не происходит»."""
    return profile_store.load_profile()


def resolve_job_profile(profile: dict, job: Job) -> dict:
    """Documents plus consent-aware questionnaire answers for one brand."""
    selected = document_rules.resolve_profile(profile, job)
    company = document_rules.brand_key(job) or str(job.brand or "sallinggroup").strip()
    return profile_store.resolve_company_answers(selected, company)


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


def _set_file_by_attrs(frame, path: str, label: str, attr_re) -> bool:
    """Ставит файл прямо в SAP file-input по id/name/aria.

    Это надёжнее клика по тексту кнопки: в форме Salling реальные поля уже имеют
    стабильные id вроде ApplyResumeUpload-fu и idCoverLetUpload-fu.
    """
    if not path or not Path(path).exists():
        return False
    try:
        inputs = frame.query_selector_all('input[type="file"]')
    except Exception:
        return False
    for fi in inputs:
        try:
            attrs = " ".join(filter(None, [
                fi.get_attribute("id"),
                fi.get_attribute("name"),
                fi.get_attribute("aria-label"),
                fi.get_attribute("title"),
                fi.get_attribute("accept"),
            ]))
        except Exception:
            attrs = ""
        if attr_re.search(attrs):
            if _set_file(fi, path, label):
                return True
    return False


def _input_attr_str(fi) -> str:
    """id/name/aria/title file-инпута — чтобы отличать поле CV от поля письма."""
    try:
        return " ".join(filter(None, [
            fi.get_attribute("id"),
            fi.get_attribute("name"),
            fi.get_attribute("aria-label"),
            fi.get_attribute("title"),
        ]))
    except Exception:
        return ""


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
    try:
        if cv:
            cv = profile_store.safe_document_upload_path(cv, "cv")
        if cover:
            cover = profile_store.safe_document_upload_path(cover, "cover_letter")
    except Exception as e:
        print(f"  warning: не смог подготовить безопасное имя файла: {str(e)[:120]}")

    # убрать подсказку про логин, если осталась
    try:
        page.evaluate("() => { const e=document.getElementById('saling-login-banner'); if(e) e.remove(); }")
    except Exception:
        pass

    clear_autofilled_emails(page)

    cover_re = re.compile(r"ansøg|cover|motivation|følgebrev", re.I)
    cv_re = re.compile(r"\bcv\b|resume|curriculum", re.I)
    cover_input_re = re.compile(r"coverlet|cover|ansøg|ansoeg|motivation|følgebrev", re.I)
    cv_input_re = re.compile(r"applyresume|resume|\bcv\b|curriculum", re.I)
    # текст КНОПОК (а не контекста)
    cover_btn_re = re.compile(r"vælg fil|vedhæft ansøg|vælg|browse|upload|attach", re.I)
    cv_btn_re = re.compile(r"tilføj cv|vælg cv|upload cv", re.I)

    frames = _all_frames(page)
    _dump_form(frames)

    cover_uploaded = False
    cv_uploaded = False

    for fr in frames:
        # 1) Сначала прямые SAP file-input по id/name.
        if cv and not cv_uploaded:
            cv_uploaded = _set_file_by_attrs(fr, cv, "CV", cv_input_re)
        if cover and not cover_uploaded:
            cover_uploaded = _set_file_by_attrs(fr, cover, "Мотивационное письмо", cover_input_re)
        # 2) Письмо — клик по кнопке «Vælg fil»
        if cover and not cover_uploaded:
            cover_uploaded = _click_upload_by_text(page, fr, cover_btn_re, cover, "Мотивационное письмо")
            if not cover_uploaded:  # фолбэк: скрытое file-поле по контексту
                try:
                    for fi in fr.query_selector_all('input[type="file"]'):
                        if cv_input_re.search(_input_attr_str(fi)):
                            continue  # не кладём письмо в поле CV
                        if cover_re.search(_context_text(fi)):
                            if _set_file(fi, cover, "Мотивационное письмо"):
                                cover_uploaded = True
                                break
                except Exception:
                    pass
        # 3) CV — клик по «Tilføj CV»
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

    # последний шанс для письма: любое ещё не использованное pdf-поле, КРОМЕ поля CV
    if cover and not cover_uploaded:
        for fr in frames:
            try:
                for fi in fr.query_selector_all('input[type="file"]'):
                    if cv_input_re.search(_input_attr_str(fi)):
                        continue  # никогда не кладём письмо в поле CV (иначе «прикрепи CV»)
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

    # Дождаться, пока сайт покажет ИМЕННО наши файлы: до этого на странице висит
    # документ от прошлой заявки (для другой сети), и отправлять/снимать скрин рано.
    wait_documents_settled(page, {
        "CV": Path(cv).name if (cv and cv_uploaded) else "",
        "письмо": Path(cover).name if (cover and cover_uploaded) else "",
    })


DOCS_SETTLE_MS = 20000        # сколько ждём подмены документов на странице
DOCS_POLL_MS = 500


def _page_mentions(page, needle: str) -> bool:
    """Есть ли имя файла в тексте страницы (во всех фреймах)."""
    if not needle:
        return True
    for fr in _all_frames(page):
        try:
            if needle.lower() in (fr.inner_text("body") or "").lower():
                return True
        except Exception:  # noqa: BLE001 — фрейм мог отвалиться
            continue
    return False


def wait_documents_settled(page, expected: dict, timeout_ms: int = DOCS_SETTLE_MS) -> bool:
    """Ждём появления имён наших файлов на странице. True — дождались всех.

    Не бросает: если сайт показывает документы иначе, просто идём дальше по
    таймауту, как было раньше.
    """
    names = {label: name for label, name in (expected or {}).items() if name}
    if not names:
        return True
    waited = 0
    while waited < timeout_ms:
        missing = [label for label, name in names.items() if not _page_mentions(page, name)]
        if not missing:
            if waited:
                print(f"  документы обновились на странице за {waited / 1000:.1f} c")
            return True
        try:
            page.wait_for_timeout(DOCS_POLL_MS)
        except Exception:  # noqa: BLE001
            return False
        waited += DOCS_POLL_MS
    print(f"  документы не подтвердились за {timeout_ms / 1000:.0f} c: "
          f"{', '.join(names.values())} — проверь вручную")
    return False


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
    try:
        from connectors.generic_apply import fill_answer_fields

        filled += len(fill_answer_fields(page, profile))
    except Exception:
        pass
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


def _ps_single_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _browser_profile_in_use() -> bool:
    if not sys.platform.startswith("win"):
        return False
    profile = str(config.BROWSER_PROFILE_DIR)
    script = (
        "$profile = " + _ps_single_quote(profile) + "; "
        "$names = @('chrome.exe','msedge.exe','chromium.exe'); "
        "$count = @(Get-CimInstance Win32_Process | Where-Object { "
        "$names -contains $_.Name -and $_.CommandLine -like ('*' + $profile + '*') "
        "}).Count; "
        "Write-Output $count"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return int((res.stdout or "0").strip() or "0") > 0
    except Exception:
        return True


def _clear_stale_browser_locks() -> bool:
    if _browser_profile_in_use():
        return False
    removed = []
    for path in config.BROWSER_PROFILE_DIR.glob("Singleton*"):
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            pass
    if removed:
        print(f"  очистил старый lock браузера: {', '.join(removed)}")
    return bool(removed)


def _is_browser_lock_error(msg: str) -> bool:
    return any(s in msg for s in (
        "has been closed", "SingletonLock", "ProcessSingleton",
        "being used by another", "DevToolsActivePort",
    ))


def _launch_browser(p):
    """Запускает ВИДИМЫЙ браузер с сохранённым профилем.

    Сначала пробует системный Chrome/Edge (channel), потому что встроенный в
    Playwright Chromium на этой машине не стартует из-за отсутствующего
    Visual C++ Redistributable («side-by-side configuration is incorrect»).
    Системные браузеры имеют все зависимости и открываются без проблем.
    """
    _clear_stale_browser_locks()
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
    def try_attempts():
        last = None
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
                return ctx, None
            except Exception as e:
                last = e
                continue
        return None, last

    ctx, last_err = try_attempts()
    if ctx:
        return ctx
    msg = str(last_err)
    if _is_browser_lock_error(msg):
        if _clear_stale_browser_locks():
            time.sleep(1)
            ctx, last_err = try_attempts()
            if ctx:
                return ctx
            msg = str(last_err)
        raise RuntimeError(
            "Похоже, браузер от прошлой подачи ещё открыт — из-за этого новый "
            "не запускается. Закрой ВСЕ окна браузера, которые открыл бот, "
            "и нажми «Запустить автоподачу» снова. Если браузер точно закрыт — "
            "подожди 5–10 секунд и попробуй ещё раз."
        )
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


def _await_submission_outcome(page, timeout_s: float = 30.0, poll_ms: int = 1000) -> str:
    """Ждёт исхода отправки, ОПРАШИВАЯ страницу до timeout_s секунд.

    Зачем: SAP-форма показывает квитанцию и убирает кнопку «Ansøg» с задержкой.
    Прежняя единичная мгновенная проверка под лагом видела «ещё не готово» и
    возвращала неуспех — заявка уходила в Salling, но applied_at НЕ записывался
    (баг F35: потеря записи и, как следствие, повторные подачи-дубли). Теперь
    даём форме время и, главное, РАЗЛИЧАЕМ силу доказательства (F35-зеркало:
    «кнопка исчезла» — не то же самое, что «сайт подтвердил приём»).

    Возвращает:
      "receipt"  — появилась страница-квитанция (надёжное подтверждение);
      "indirect" — квитанции не было, но кнопка «Ansøg» СТАБИЛЬНО исчезла
                   (вероятно подано: сбой сессии/редирект дают ту же картину);
      "none"     — за весь таймаут ни квитанции, ни ухода формы.
    """
    stable_needed_s = 2.0          # кнопка должна пропасть устойчиво, а не мигнуть
    gone_for_s = None
    elapsed_s = 0.0
    while elapsed_s < timeout_s:
        if _submission_confirmed(page):
            return "receipt"
        # поздний диалог согласия иногда всплывает уже после клика — гасим
        try:
            accept_consent(page)
        except Exception:
            pass
        if not _ansog_present(page):
            gone_for_s = (gone_for_s or 0.0) + poll_ms / 1000.0
            if gone_for_s >= stable_needed_s:
                # кнопка ушла надолго — но подождём квитанцию ещё немного:
                # она сильнее, и часто дорисовывается через пару секунд
                for _ in range(3):
                    if _submission_confirmed(page):
                        return "receipt"
                    page.wait_for_timeout(poll_ms)
                    elapsed_s += poll_ms / 1000.0
                return "indirect"
        else:
            gone_for_s = None       # кнопка снова видна — сброс «устойчивости»
        page.wait_for_timeout(poll_ms)
        elapsed_s += poll_ms / 1000.0
    # финальная проверка на границе дедлайна
    if _submission_confirmed(page):
        return "receipt"
    return "indirect" if not _ansog_present(page) else "none"


def submit_application(page) -> str:
    """Жмёт «Ansøg», подтверждает согласие и проверяет, что заявка реально ушла.
    Возвращает исход: "receipt" (квитанция) / "indirect" (форма ушла без
    квитанции — вероятно подано) / "none" (отправка не подтвердилась)."""
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
        return "none"
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
    # Опрашиваем исход с запасом по времени: SAP подтверждает с лагом, единичная
    # проверка теряла подтверждение → applied_at не записывался (F35).
    outcome = _await_submission_outcome(page)
    print({
        "receipt": "  ✔ отправка подтверждена квитанцией",
        "indirect": "  ✔ форма ушла (квитанции не видел) — вероятно подано, проверь письмо от Salling",
        "none": "  ⚠ отправка НЕ подтвердилась — проверь вручную",
    }[outcome])
    return outcome


def _save_proof(page, job, subdir: str = "applied"):
    """Сохраняет скриншот результата в logs/<subdir>/ как доказательство.
    Возвращает путь к файлу (или None) — его же отправляем в чат телефона.
    subdir="prepared" — прогон без отправки: такие скрины НЕ должны попадать в
    журнал подач как доказательство отправки."""
    try:
        from datetime import datetime
        out = config.DATA_DIR / "logs" / subdir
        out.mkdir(parents=True, exist_ok=True)
        rid = job.requisition_id or job.id
        name = f"{datetime.now():%Y%m%d_%H%M%S}_{rid}.png"
        path = out / name
        page.screenshot(path=str(path), full_page=True)
        print(f"  📸 скрин-пруф: logs/{subdir}/{name}")
        return path
    except Exception as e:
        print("  не смог сохранить скрин:", e)
        return None


# Телеграм не любит очень длинные картинки (страница целиком бывает в десятки
# тысяч пикселей), да и смотреть их с телефона неудобно. Берём верх страницы —
# там и висит подтверждение сайта — ужимаем по ширине и жмём в JPEG.
PROOF_MAX_W = 1000
PROOF_MAX_H = 2400
PROOF_MAX_BYTES = 2_600_000


def _proof_photo_b64(path) -> str:
    """PNG со страницы → компактный JPEG в base64. Пусто, если не вышло."""
    try:
        import base64
        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("RGB")
            if im.width > PROOF_MAX_W:
                h = max(1, round(im.height * PROOF_MAX_W / im.width))
                im = im.resize((PROOF_MAX_W, h), Image.LANCZOS)
            if im.height > PROOF_MAX_H:
                im = im.crop((0, 0, im.width, PROOF_MAX_H))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=78, optimize=True)
        raw = buf.getvalue()
        if len(raw) > PROOF_MAX_BYTES:
            return ""
        return base64.b64encode(raw).decode("ascii")
    except Exception as e:  # noqa: BLE001 — пруф не должен ронять подачу
        print("  скрин не подготовился к отправке:", str(e)[:120])
        return ""


def _cloud_proof(job, path, confidence: str = "receipt", ask_send: bool = False) -> None:
    """Отправить скрин-доказательство в чат Telegram. Никогда не падает."""
    if not path:
        return
    try:
        b64 = _proof_photo_b64(path)
        if not b64:
            return
        where = " · ".join(x for x in [getattr(job, "brand", ""), getattr(job, "city", "")] if x)
        if confidence == "prepared":
            head = "🧪 <b>Анкета подготовлена</b>"
            tail = ("Проверь, всё ли верно, и решай кнопками ниже: отправлю или закрою."
                    if ask_send else
                    "Заполнена на компьютере, отправка НЕ нажата — проверь и реши сам.")
        elif confidence == "receipt":
            head = "✅ <b>Заявка отправлена</b>"
            tail = "Скрин страницы сразу после отправки."
        else:
            head = "⚠️ <b>Отправлено без квитанции</b>"
            tail = "Сайт не показал квитанцию — проверь письмо или кабинет."
        caption = f"{head}\n{getattr(job, 'title', '') or 'Вакансия'}"
        if where:
            caption += f"\n{where}"
        caption += f"\n{tail}"
        import cloud_auth
        cloud_auth.report_apply_proof(str(job.id), b64, caption, ask_send=ask_send)
    except Exception as e:  # noqa: BLE001
        print("  скрин не ушёл в чат:", str(e)[:120])


def _mark_applied(job_id: str, confidence: str = "receipt"):
    """Отмечает вакансию как поданную после реальной отправки.

    confidence — способ подтверждения для журнала доверия: "receipt" (квитанция)
    или "indirect" (форма ушла без квитанции). Оба варианта записываются как
    applied + applied_at: даже «вероятная» подача скорее всего дошла до Salling,
    и повторная отправка на неё опаснее, чем лишняя пометка (дубли — хуже).

    Это критично: без applied_at вакансия снова станет «подходящей» и может уйти
    повторно. Поэтому при сбое (например, база кратковременно занята синком)
    повторяем несколько раз с нарастающей паузой, а не сдаёмся с первого раза (F34)."""
    import time as _time
    from db import utcnow
    last_err = None
    for attempt in range(5):
        try:
            with get_session() as s:
                j = s.get(Job, job_id)
                if j:
                    j.status = "applied"
                    j.applied_at = utcnow()
                    j.applied_confidence = confidence
                    s.add(j)
                    s.commit()
            print("  ✔ отмечено «подано» в дашборде"
                  + (" (косвенное подтверждение — проверь письмо)" if confidence == "indirect" else ""))
            return
        except Exception as e:  # noqa: BLE001 — повторяем, отметка важнее
            last_err = e
            _time.sleep(0.5 * (attempt + 1))
    print(f"  ⚠ НЕ смог отметить applied после повторов — проверь вручную: {last_err}")


def _applied_confidence(job_id: str) -> str:
    """Вернуть сохранённый способ подтверждения заявки."""
    try:
        with get_session() as s:
            job = s.get(Job, str(job_id))
            return str(getattr(job, "applied_confidence", "") or "").strip().lower()
    except Exception:
        return ""


def _job_for_ai(job: Job) -> dict:
    return {
        "title": job.title or "",
        "brand": job.brand or "",
        "city": job.city or "",
        "description": job.description or "",
    }


def _run_ai_fill(page, profile: dict, job: Job, enabled: bool) -> list[dict]:
    if not enabled:
        return []
    try:
        from connectors import ai_fill
        results = ai_fill.fill(page, profile, job=_job_for_ai(job))
        if results:
            drafts = sum(1 for item in results if item.get("kind") == "draft")
            facts = len(results) - drafts
            print(f"  ИИ-проверка: заполнено фактов {facts}, черновиков {drafts}")
        else:
            print("  ИИ-проверка: новых полей для заполнения нет")
        return results
    except Exception as exc:  # AI must never block a normal application
        print("  ИИ-проверка пропущена:", str(exc)[:120])
        return []


def _log_document_selection(profile: dict) -> None:
    selection = profile.get("_document_selection") or {}
    cv = (selection.get("cv") or {}).get("label", "Общий комплект")
    cover = (selection.get("cover") or {}).get("label", "Общий комплект")
    print(f"  документы: CV — {cv}; письмо — {cover}")


PHONE_CONFIRM_WAIT_S = 900     # 15 минут ждём решение из телефона
_close_requested = False       # человек нажал «Отмена» — браузер не держим открытым


def read_phone_decision(job_id) -> str:
    """Забрать решение из телефона: "submit" | "cancel" | "" (ещё не нажал)."""
    path = config.prepare_signal_path(job_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    try:
        path.unlink()
    except OSError:
        pass
    action = str((data or {}).get("action") or "").strip().lower()
    return action if action in ("submit", "cancel") else ""


def wait_phone_decision(page, job, timeout_s: int = PHONE_CONFIRM_WAIT_S) -> str:
    """Держим анкету открытой и ждём кнопку из чата. Пусто — не дождались."""
    print("  жду решение из телефона: «Отправить» или «Отмена»…")
    waited = 0
    while waited < timeout_s:
        action = read_phone_decision(job.id)
        if action:
            print(f"  из телефона пришло: {action}")
            return action
        try:
            page.wait_for_timeout(2000)
        except Exception:  # noqa: BLE001 — окно закрыли руками
            return ""
        waited += 2
    print("  решение из телефона не пришло — оставляю анкету открытой")
    return ""


def process_job(page, job, profile, submit: bool, ai_fill: bool = False,
                phone_confirm: bool = False):
    """Открывает вакансию, ждёт логин, открывает форму, грузит файлы, опц. отправляет."""
    print(f"\n=== {job.title} — {job.city} ===\n{job.application_link}")
    _log_document_selection(profile)
    sent = False
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
        _run_ai_fill(page, profile, job, ai_fill)
    except Exception as e:
        print("  warning:", e)
    if submit:
        try:
            outcome = submit_application(page)
        except Exception as e:
            print("  отправка сорвалась:", str(e)[:120])
            outcome = "none"
        if outcome in ("receipt", "indirect"):
            print("  ОТПРАВЛЕНО ✔" if outcome == "receipt" else "  ВЕРОЯТНО ОТПРАВЛЕНО — проверь письмо от Salling")
            proof = _save_proof(page, job)
            _mark_applied(job.id, confidence=outcome)
            _cloud_proof(job, proof, outcome)
            sent = True
        else:
            _save_proof(page, job)   # скрин даже при неуспехе — для разбора/восстановления
            # Защита: если формы Salling больше нет в привычном виде — говорим
            # человеку правду («сайт изменился, нужно обновление»), а не общее
            # «не подтвердилось». Проверяем ТОЛЬКО после неудачи: до неё
            # придирки контракта могли бы зря остановить рабочую подачу.
            guard = site_contract.check(page, "salling")
            if not guard["ok"]:
                print("  ЗАЩИТА:", site_contract.human_message(guard).replace("\n", " "))
                _cloud_report(job.id, "failed", site_contract.short_message(guard))
            else:
                print("  отправка не подтвердилась — проверь вручную")
    else:
        print("  Прогон без отправки — проверь форму и нажми Ansøg сам.")
        # хвост прошлого прогона не должен сработать за человека — чистим ДО
        # того, как отправим скрин с кнопками
        if phone_confirm:
            read_phone_decision(job.id)
        # телефон должен видеть, что прогон реально дошёл до заполненной формы
        _cloud_proof(job, _save_proof(page, job, subdir="prepared"), "prepared",
                     ask_send=phone_confirm)
        if phone_confirm:
            sent = _finish_by_phone(page, job)
    return sent


def _finish_by_phone(page, job) -> bool:
    """Дождаться кнопки из чата и довести анкету до конца (или закрыть).

    Отправку запускает ЧЕЛОВЕК — кнопкой под скрином, увидев, что всё верно.
    """
    global _close_requested
    action = wait_phone_decision(page, job)
    if action == "cancel":
        print("  отмена из телефона — заявка НЕ отправлена, закрываю анкету")
        _cloud_report(job.id, "prepare_cancelled",
                      "Отменено с телефона — заявка не отправлена, окно закрыто.")
        _close_requested = True
        return False
    if action != "submit":
        return False
    print("  подтверждение из телефона — отправляю заявку")
    try:
        outcome = submit_application(page)
    except Exception as e:  # noqa: BLE001
        print("  отправка сорвалась:", str(e)[:120])
        outcome = "none"
    if outcome in ("receipt", "indirect"):
        proof = _save_proof(page, job)
        _mark_applied(job.id, confidence=outcome)
        _cloud_report(
            job.id,
            "submitted" if outcome == "receipt" else "unconfirmed",
            "Сайт показал квитанцию и подтвердил получение заявки."
            if outcome == "receipt" else
            "Форма исчезла, но сайт не показал квитанцию. Проверь письмо или кабинет Salling.",
        )
        _cloud_proof(job, proof, outcome)
        _close_requested = True
        return True
    _save_proof(page, job)
    _cloud_report(job.id, "failed",
                  "Отправка не подтвердилась — анкета осталась открытой на ПК.")
    return False


def run(job_id: str | None, login_only: bool = False, web_mode: bool = False,
        submit: bool = False, keep_open: bool = True, ai_fill: bool = False):
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
            job_profile = resolve_job_profile(profile, job)
            process_job(page, job, job_profile, submit, ai_fill=ai_fill)

        if web_mode and keep_open:
            print("\n>>> Браузер останется открытым. Закрой его, когда закончишь.")
            _wait_until_browser_closed(ctx)
        elif not web_mode and keep_open:
            input("\nНажми Enter здесь, когда закончишь, чтобы закрыть браузер...")
        ctx.close()


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# ── Прогресс пакетной подачи ────────────────────────────────────────────
# Воркер пишет живой прогресс в apply_progress.json — дашборд приложения и
# (через облако) Mini App в Telegram показывают: сколько всего, сколько подано,
# какая вакансия сейчас, что не удалось.
PROGRESS_PATH = config.DATA_DIR / "apply_progress.json"


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _write_progress(state: dict) -> None:
    """Атомарно записать прогресс (через .tmp + replace), чтобы дашборд не читал
    наполовину записанный файл."""
    try:
        import os
        tmp = str(PROGRESS_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, str(PROGRESS_PATH))
    except Exception:
        pass


def _prepare_report(job_error: str = "") -> tuple[str, str]:
    """Итог прогона «Подготовить» для телефона.

    process_job возвращает «отправлено ли», а в прогоне мы намеренно НЕ
    отправляем — значит False там всегда. Успех прогона = дошли до формы без
    ошибки. Пока это считалось по process_job, панель писала «Прогон не удался»
    даже над идеально заполненной анкетой.
    """
    if job_error:
        return ("prepare_failed",
                f"Не получилось заполнить анкету: {job_error} — открой WexFlow на ПК и посмотри.")
    return ("prepared", "Анкета заполнена и ждёт тебя на ПК — отправка НЕ нажата.")


def _cloud_report(job_id, state: str, msg: str = "") -> None:
    """Best-effort: сообщить облаку статус заявки, чтобы карточка в Mini App
    обновлялась вживую и для подачи из самого приложения тоже. Никогда не падает."""
    try:
        import cloud_auth
        cloud_auth.report_apply_result(str(job_id), state, msg)
    except Exception:
        pass


def _cloud_progress(prog: dict) -> None:
    """Best-effort: отправить в облако компактную сводку прогресса пачки — для
    баннера прогресса в Mini App. Шлём только лёгкие поля, без всего списка."""
    try:
        import cloud_auth
        cloud_auth.report_apply_progress({
            "active": prog.get("active"),
            "mode": prog.get("mode"),
            "total": prog.get("total"),
            "done": prog.get("done"),
            "ok": prog.get("ok"),
            "unconfirmed": prog.get("unconfirmed"),
            "failed": prog.get("failed"),
            "current": prog.get("current"),
            "updated_at": prog.get("updated_at"),
            "finished_at": prog.get("finished_at"),
        })
    except Exception:
        pass


def run_batch(job_ids, submit: bool = False, web_mode: bool = True,
              concurrency: int = 1, keep_open: bool = True, ai_fill: bool = False,
              phone_confirm: bool = False):
    """Пакетная подача.

    Реальная отправка всегда идёт последовательно в одной вкладке: так меньше
    шансов потерять браузерный контекст между заявками. Параллельные вкладки
    остаются только запасным режимом для ручного dry-run.
    """
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
    effective_concurrency = 1 if submit else max(1, concurrency)
    print(f"Пакетная подача: вакансий {len(jobs)}, по {effective_concurrency} за раз, "
          f"{'С ОТПРАВКОЙ' if submit else 'прогон без отправки'}, "
          f"ИИ {'включён' if ai_fill else 'выключен'}")

    submitted = 0
    confirmed = 0
    unconfirmed = 0
    with sync_playwright() as p:
        ctx = _launch_browser(p)
        if effective_concurrency <= 1:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            items = [{"id": j.id, "title": j.title, "city": j.city, "state": "pending"} for j in jobs]
            prog = {
                "active": True, "mode": "submit" if submit else "dry",
                "total": len(jobs), "done": 0, "ok": 0, "unconfirmed": 0, "failed": 0,
                "current": None, "items": items,
                "started_at": _now_iso(), "updated_at": _now_iso(), "finished_at": None,
            }
            _write_progress(prog)
            # Прогресс шлём и для прогона без отправки: телефон — пульт, человек
            # должен видеть, что компьютер работает, а не гадать (mode="dry").
            _cloud_progress(prog)
            for i, job in enumerate(jobs):
                if page.is_closed():
                    page = ctx.new_page()
                prog["current"] = {"idx": i + 1, "id": job.id, "title": job.title, "city": job.city}
                items[i]["state"] = "submitting"
                prog["updated_at"] = _now_iso()
                _write_progress(prog)
                if submit:
                    _cloud_report(job.id, "submitting", "WexFlow заполняет форму")
                else:
                    _cloud_report(job.id, "preparing",
                                  "WexFlow заполняет анкету — отправку не нажимает")
                ok = False
                job_error = ""
                try:
                    job_profile = resolve_job_profile(profile, job)
                    ok = process_job(page, job, job_profile, submit, ai_fill=ai_fill,
                                     phone_confirm=phone_confirm)
                except Exception as e:  # одна вакансия не должна валить всю пачку
                    print("  job error:", str(e)[:120])
                    ok = False
                    job_error = str(e)[:120]
                if submit:
                    if ok:
                        submitted += 1
                        confidence = _applied_confidence(job.id)
                        if confidence == "receipt":
                            confirmed += 1
                            items[i]["state"] = "ok"
                            prog["ok"] += 1
                            _cloud_report(
                                job.id, "submitted",
                                "Сайт показал квитанцию и подтвердил получение заявки.",
                            )
                        else:
                            unconfirmed += 1
                            items[i]["state"] = "unconfirmed"
                            prog["unconfirmed"] += 1
                            _cloud_report(
                                job.id, "unconfirmed",
                                "Форма исчезла, но сайт не показал квитанцию. Проверь письмо или кабинет Salling.",
                            )
                    else:
                        items[i]["state"] = "failed"
                        prog["failed"] += 1
                        _cloud_report(job.id, "failed", "Подача не подтверждена — проверь вручную")
                else:
                    # ВАЖНО: в прогоне process_job ВСЕГДА возвращает False — он
                    # возвращает «отправлено ли», а мы намеренно не отправляем.
                    # Раньше телефон из-за этого писал «Прогон не удался» даже
                    # над идеально заполненной анкетой. Успех прогона = дошли до
                    # формы без ошибки.
                    items[i]["state"] = "done" if job_error else "ok"
                    _cloud_report(job.id, *_prepare_report(job_error))
                prog["done"] = i + 1
                prog["updated_at"] = _now_iso()
                _write_progress(prog)
                _cloud_progress(prog)
                if submit:
                    try:
                        page.goto("about:blank", wait_until="domcontentloaded", timeout=10000)
                    except Exception:
                        if page.is_closed():
                            page = ctx.new_page()
            prog["active"] = False
            prog["current"] = None
            prog["finished_at"] = _now_iso()
            prog["updated_at"] = _now_iso()
            _write_progress(prog)
            _cloud_progress(prog)
            if submit:
                print(
                    f"\n========\nИТОГ: сайт подтвердил квитанцией: {confirmed} из {len(jobs)}; "
                    f"без квитанции: {unconfirmed}; не подтверждено: {len(jobs) - submitted}."
                )
                if unconfirmed:
                    print("Заявки без квитанции сохранены от дублей, но их нужно проверить по письму или кабинету.")
            else:
                print(f"\n========\nПрогон завершён ({len(jobs)} вакансий обработано) — НЕ отправлял, ничего не отмечал.")

            if web_mode and keep_open and not _close_requested:
                print("\n>>> Готово. Браузер остаётся открытым — проверь/закрой сам.")
                _wait_until_browser_closed(ctx)
            ctx.close()
            return

        # закрыть стартовую пустую вкладку позже; пока используем новые
        for batch in _chunks(jobs, effective_concurrency):
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
                job_profile = resolve_job_profile(profile, job)
                _log_document_selection(job_profile)
                try:
                    wait_for_login_if_needed(pg, job_profile)
                    add_job_banner(pg, job)
                    wait_for_application_form(pg, job)
                    accept_consent(pg)
                    add_job_banner(pg, job)
                    upload_documents(pg, job_profile)
                    _run_ai_fill(pg, job_profile, job, ai_fill)
                    if submit:
                        outcome = submit_application(pg)
                        if outcome in ("receipt", "indirect"):
                            print("  ОТПРАВЛЕНО ✔" if outcome == "receipt" else "  ВЕРОЯТНО ОТПРАВЛЕНО — проверь письмо")
                            _save_proof(pg, job)
                            _mark_applied(job.id, confidence=outcome)
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

        if web_mode and keep_open:
            print("\n>>> Готово. Браузер остаётся открытым — проверь/закрой сам.")
            _wait_until_browser_closed(ctx)
        ctx.close()


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        sys.exit("Использование: python apply.py <job_id> [<job_id> ...] [--submit] [--web] [--auto-close] | python apply.py --login")
    # Страховка: если воркер запущен раньше, чем приложение успело мигрировать
    # базу после обновления (или вручную), — доводим схему сами, а не падаем
    # на «no such column» посреди подачи.
    init_db()
    web_mode = "--web" in args
    submit = "--submit" in args
    ai_fill = "--ai-fill" in args
    phone_confirm = "--phone-confirm" in args
    keep_open = "--auto-close" not in args
    ids = [a for a in args if not a.startswith("--")]
    if "--login" in args:
        run(None, login_only=True, web_mode=web_mode, keep_open=keep_open)
    elif len(ids) > 1 or (ids and (submit or web_mode)):
        # Любой запуск из приложения (web_mode) и реальная отправка даже ОДНОГО id
        # идут через run_batch: только он пишет apply_progress.json, который читает
        # полоска прогресса (шаг 4 — воркер возвращает результат, а не «приложение
        # угадывает по базе»). Раньше одиночный dry шёл через run() — прогресс
        # в интерфейсе оставался пустым.
        run_batch(ids, submit=submit, web_mode=web_mode, keep_open=keep_open, ai_fill=ai_fill,
                  phone_confirm=phone_confirm)
    else:
        run(ids[0], web_mode=web_mode, submit=submit, keep_open=keep_open, ai_fill=ai_fill)


if __name__ == "__main__":
    main()
