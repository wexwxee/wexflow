"""Общие куски для всех заполнителей коннекторов (cookie-баннер, загрузка CV,
баннер-инструкция, профиль). Используют и teamtailor_apply, и generic_apply."""
from __future__ import annotations

import re
from pathlib import Path

import profile_store


def load_profile() -> dict:
    """Read the same canonical profile that the WexFlow UI saves."""
    profile = profile_store.load_profile()
    if not isinstance(profile, dict):
        return {}
    return profile


def _control_text(control) -> str:
    """Accessible label plus stable attributes for a form control."""
    try:
        return str(control.evaluate(
            """e => {
                const bits = [e.name, e.id, e.getAttribute('aria-label'),
                  e.placeholder, e.getAttribute('data-qa'), e.getAttribute('data-testid')];
                if (e.labels) for (const label of e.labels) bits.push(label.innerText);
                const group = e.closest('label,.field,.form-group,[data-field],fieldset');
                if (group) {
                  const label = group.matches('label') ? group : group.querySelector('label,legend');
                  if (label) bits.push(label.innerText);
                }
                return bits.filter(Boolean).join(' ').toLowerCase();
            }"""
        ) or "")
    except Exception:
        return ""


_CV_RX = re.compile(r"(?:^|\W)cv(?:\W|$)|r[ée]sum[ée]?|curriculum|lebenslauf", re.I)
_COVER_RX = re.compile(r"cover|letter|motiv|ansøgning|følgebrev|application.?letter", re.I)
_OTHER_FILE_RX = re.compile(r"photo|avatar|image|portfolio|certificate|transcript", re.I)


def _file_input(page, kind: str):
    """Pick a file control by its accessible label; never guess among ambiguous inputs."""
    try:
        controls = page.locator('input[type="file"]').all()
    except Exception:
        return None
    described = [(control, _control_text(control)) for control in controls]
    wanted = _COVER_RX if kind == "cover" else _CV_RX
    for control, text in described:
        if wanted.search(text):
            return control
    if kind == "cv":
        neutral = [
            control for control, text in described
            if not _COVER_RX.search(text) and not _OTHER_FILE_RX.search(text)
        ]
        if len(neutral) == 1:
            return neutral[0]
    return None


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
        control = _file_input(page, "cv")
        if control is not None:
            control.set_input_files(cv)
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
        control = _file_input(page, "cover")
        if control is not None:
            control.set_input_files(cl)
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
                    if(e.type==='checkbox' || e.type==='radio') {
                      if(e.checked) return;
                      if(e.type==='radio' && e.name && document.querySelector(`input[type="radio"][name="${CSS.escape(e.name)}"]:checked`)) return;
                    } else if((e.value||'').trim()) return;
                    let lab=e.getAttribute('aria-label')||e.placeholder||'';
                    if(!lab && e.id){const l=document.querySelector(`label[for="${e.id}"]`); if(l) lab=l.innerText;}
                    if(!lab && e.labels && e.labels.length) lab=e.labels[0].innerText;
                    if(!lab){const g=e.closest('fieldset,.field,.form-group,[data-field]'); const l=g&&g.querySelector('legend,label'); if(l) lab=l.innerText;}
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
    """Isolated floating summary: filled fields, remaining work and safety boundary."""
    payload = {
        "platform": platform or "Форма",
        "filled": list(filled or []),
        "missing": list(missing or []),
        "questions": int(questions or 0),
    }
    try:
        page.evaluate(
            """(data) => {
                const id='wexflow-banner';
                const old=document.getElementById(id); if(old) old.remove();
                const host=document.createElement('div'); host.id=id;
                host.style.cssText='position:fixed;top:16px;right:16px;z-index:2147483647;'
                  +'width:min(420px,calc(100vw - 32px));color-scheme:dark;';
                const root=host.attachShadow({mode:'open'});
                root.innerHTML=`<style>
                  *{box-sizing:border-box} .card{font:13px/1.42 Inter,Segoe UI,sans-serif;color:#e9efeb;
                    background:#111513;border:1px solid #304039;border-radius:14px;padding:14px;
                    box-shadow:0 18px 60px rgba(0,0,0,.45)}
                  .head{display:flex;align-items:center;gap:9px;margin-bottom:10px}.mark{color:#1ed760;font-size:17px}
                  .title{flex:1;font-weight:800;font-size:14px}.platform{color:#96a39c;font-size:11px;font-weight:600}
                  button{border:0;background:#222a26;color:#b9c3bd;border-radius:7px;width:27px;height:27px;cursor:pointer;font-size:17px}
                  .row{margin-top:7px;padding:8px 10px;border-radius:9px;background:#19201c;color:#bdc7c1}
                  .ok{border:1px solid #235f39;background:#102a1a;color:#8ff0ae}.warn{border:1px solid #66511e;background:#29230f;color:#f5d778}
                  .label{font-weight:800}.foot{margin-top:10px;color:#aab4ae;font-size:11.5px}
                </style><section class="card" role="status"><div class="head"><span class="mark">◆</span>
                  <div class="title">WexFlow · форма подготовлена<div class="platform"></div></div>
                  <button type="button" aria-label="Закрыть">×</button></div>
                  <div class="row ok"><span class="label">Заполнено:</span> <span class="filled"></span></div>
                  <div class="row questions" hidden></div><div class="row warn missing" hidden></div>
                  <div class="foot">Проверь данные, поставь нужные согласия и отправь анкету сам. WexFlow не нажимает финальную кнопку.</div>
                </section>`;
                root.querySelector('.platform').textContent=data.platform;
                root.querySelector('.filled').textContent=data.filled.length?data.filled.join(', '):'распознанных полей нет';
                const q=root.querySelector('.questions'); if(data.questions){q.hidden=false;q.textContent=`Дополнительных вопросов: ${data.questions}`;}
                const m=root.querySelector('.missing'); if(data.missing.length){m.hidden=false;m.textContent=`Осталось заполнить: ${data.missing.join(', ')}`;}
                root.querySelector('button').addEventListener('click',()=>host.remove());
                document.documentElement.appendChild(host);
            }""",
            payload,
        )
    except Exception:
        pass
