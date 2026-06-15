"""Общие куски для всех заполнителей коннекторов (cookie-баннер, загрузка CV,
баннер-инструкция, профиль). Используют и teamtailor_apply, и generic_apply."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import paths

PROFILE_PATH = paths.DATA_DIR / "profile.json"


def load_profile() -> dict:
    if not PROFILE_PATH.exists():
        sys.exit("Нет profile.json — заполни профиль в приложении.")
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def dismiss_cookies(page) -> None:
    """Закрыть cookie-баннер, отклоняя необязательные (privacy-preserving)."""
    for sel in (
        '[data-action*="disableAll"]',
        'button:has-text("Afvis")', 'button:has-text("Decline")',
        'button:has-text("Kun nødvendige")', 'button:has-text("Only necessary")',
        'button:has-text("Reject all")', 'button:has-text("Accepter alle")',
    ):
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible():
                el.click(timeout=2500)
                page.wait_for_timeout(400)
                return
        except Exception:
            continue


def upload_cv(page, profile: dict) -> bool:
    """Прикрепить CV: сперва прямой input[type=file] (в т.ч. скрытый), иначе клик
    по кнопке загрузки с перехватом выбора файла."""
    cv = (profile.get("cv_path") or "").strip()
    if not cv or not Path(cv).exists():
        print("  CV не найден в профиле — пропускаю загрузку.")
        return False
    try:
        inputs = page.locator('input[type="file"]')
        if inputs.count():
            inputs.first.set_input_files(cv)
            print(f"  CV загружен: {Path(cv).name}")
            return True
    except Exception:
        pass
    for sel in (
        'button:has-text("Upload")', 'button:has-text("Vedhæft")',
        'button:has-text("Vælg fil")', 'button:has-text("Attach")',
        'label:has-text("CV")', 'button:has-text("resume")', 'button:has-text("résumé")',
    ):
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible():
                with page.expect_file_chooser(timeout=5000) as fc:
                    el.click()
                fc.value.set_files(cv)
                print(f"  CV прикреплён: {Path(cv).name}")
                return True
        except Exception:
            continue
    print("  Кнопку загрузки CV не нашёл — прикрепи вручную.")
    return False


def attach_cover_letter(page, profile: dict) -> bool:
    """Прикрепить сопроводительное ФАЙЛОМ, если на форме есть отдельный input
    под него (по подписи cover/letter/motivation). К резюме не лезем."""
    cl = (profile.get("cover_letter_path") or "").strip()
    if not cl or not Path(cl).exists():
        return False
    try:
        for fi in page.locator('input[type="file"]').all():
            attrs = (fi.get_attribute("name") or "") + (fi.get_attribute("id") or "") + \
                    (fi.get_attribute("aria-label") or "")
            if re.search(r"cover|letter|motiv|ansøgning|følgebrev", attrs, re.I):
                fi.set_input_files(cl)
                print(f"  сопроводительное прикреплено: {Path(cl).name}")
                return True
    except Exception:
        pass
    return False


def missing_required(page) -> list[str]:
    """Подписи обязательных, но пустых полей — чтобы человек знал, что дозаполнить."""
    try:
        return page.evaluate(
            """() => {
                const out=[];
                document.querySelectorAll('input[required],textarea[required],select[required]').forEach(e=>{
                    if(e.type==='hidden'||e.offsetParent===null) return;
                    const v=(e.value||'').trim();
                    if(v) return;
                    let lab=e.getAttribute('aria-label')||e.placeholder||'';
                    if(!lab && e.id){const l=document.querySelector(`label[for="${e.id}"]`); if(l) lab=l.innerText;}
                    lab=(lab||e.name||'поле').trim().slice(0,40);
                    if(lab && !out.includes(lab)) out.push(lab);
                });
                return out.slice(0,8);
            }"""
        )
    except Exception:
        return []


def add_banner(page, questions: int, filled: list[str], platform: str = "",
               missing: list[str] | None = None) -> None:
    """Жёлтая плашка сверху: что сделал бот и что нужно от человека."""
    tag = f"[{platform}] " if platform else ""
    msg_q = f"Вопросов по вакансии: {questions}. " if questions else ""
    msg_m = ("Дозаполни: " + ", ".join(missing) + ". ") if missing else ""
    text = (
        f"WexFlow {tag}заполнил: " + (", ".join(filled) if filled else "—") + ". "
        + msg_q + msg_m
        + "Поставь согласие и нажми «Отправить» САМ — бот этого не делает."
    )
    try:
        page.evaluate(
            """(t) => {
                const id='wexflow-banner';
                const old=document.getElementById(id); if(old) old.remove();
                const b=document.createElement('div');
                b.id=id; b.textContent=t;
                b.style.cssText='position:fixed;top:0;left:0;right:0;z-index:2147483647;'
                  +'background:#1ed760;color:#08210f;font:600 14px/1.4 Segoe UI,sans-serif;'
                  +'padding:12px 18px;box-shadow:0 2px 12px rgba(0,0,0,.3);text-align:center;';
                document.body.appendChild(b);
                document.body.style.paddingTop='52px';
            }""",
            text,
        )
    except Exception:
        pass
