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


def show_ai_progress(
    page,
    step: int,
    total: int,
    title: str,
    detail: str = "",
    *,
    state: str = "working",
) -> None:
    """Показать/обновить живой этап ИИ-подготовки прямо поверх внешней формы.

    Состояние также сохраняется в window.__wexflowAiRun: финальный баннер читает
    его и честно показывает, завершилась ИИ-проверка или была пропущена с ошибкой.
    """
    total = max(1, int(total or 1))
    step = max(0, min(total, int(step or 0)))
    state = state if state in {"working", "done", "error"} else "working"
    payload = {
        "step": step,
        "total": total,
        "percent": round(step * 100 / total),
        "title": str(title or "Подготавливаю форму"),
        "detail": str(detail or ""),
        "state": state,
    }
    try:
        page.evaluate(
            """(data) => {
                window.__wexflowAiRun=data;
                const id='wexflow-banner';
                let host=document.getElementById(id);
                let root=host && host.shadowRoot;
                if(!root || !root.querySelector('.ai-progress-card')){
                  if(host) host.remove();
                  host=document.createElement('div'); host.id=id;
                  host.style.cssText='position:fixed;top:16px;right:16px;z-index:2147483647;'
                    +'width:min(420px,calc(100vw - 32px));color-scheme:dark;';
                  root=host.attachShadow({mode:'open'});
                  root.innerHTML=`<style>
                    *{box-sizing:border-box}.ai-progress-card{font:13px/1.42 Inter,Segoe UI,sans-serif;
                      color:#e9efeb;background:#111513;border:1px solid #304039;border-radius:14px;
                      padding:14px;box-shadow:0 18px 60px rgba(0,0,0,.45)}
                    .head{display:flex;align-items:flex-start;gap:10px}.mark{width:26px;height:26px;
                      display:grid;place-items:center;border-radius:8px;background:#142d20;color:#5bf08b;
                      font-weight:900;flex:0 0 auto}.copy{min-width:0;flex:1}.title{font-weight:800;font-size:14px}
                    .detail{margin-top:3px;color:#aab4ae;font-size:12px}.count{color:#96a39c;font-size:11px;
                      font-weight:700;white-space:nowrap}.track{height:7px;margin-top:12px;border-radius:999px;
                      background:#28312c;overflow:hidden}.bar{height:100%;width:0;border-radius:inherit;
                      background:linear-gradient(90deg,#1ed760,#38a8ff);transition:width .28s ease}
                    .working .bar{position:relative}.working .bar:after{content:"";position:absolute;inset:0;
                      background:linear-gradient(100deg,transparent,rgba(255,255,255,.45),transparent);
                      animation:sweep 1.2s linear infinite}.done{border-color:#235f39}.error{border-color:#76572b}
                    .error .mark{background:#33240e;color:#f5d778}.error .bar{background:#d59b32}
                    @keyframes sweep{from{transform:translateX(-100%)}to{transform:translateX(100%)}}
                    @media(prefers-reduced-motion:reduce){.bar{transition:none}.working .bar:after{animation:none}}
                  </style><section class="ai-progress-card" role="status" aria-live="polite">
                    <div class="head"><span class="mark">AI</span><div class="copy">
                      <div class="title"></div><div class="detail"></div></div><span class="count"></span></div>
                    <div class="track" role="progressbar" aria-valuemin="0" aria-valuemax="100">
                      <div class="bar"></div></div></section>`;
                  document.documentElement.appendChild(host);
                }
                const card=root.querySelector('.ai-progress-card');
                card.className='ai-progress-card '+data.state;
                root.querySelector('.title').textContent=data.title;
                const detail=root.querySelector('.detail');
                detail.textContent=data.detail; detail.hidden=!data.detail;
                root.querySelector('.count').textContent=data.step+' / '+data.total;
                const track=root.querySelector('.track');
                track.setAttribute('aria-valuenow',String(data.percent));
                root.querySelector('.bar').style.width=data.percent+'%';
            }""",
            payload,
        )
    except Exception:
        pass


def add_banner(page, questions: int, filled: list[str], platform: str = "",
               missing: list[str] | None = None, ai_details: list[dict] | None = None) -> None:
    """Isolated floating summary: filled fields, remaining work and safety boundary.
    ai_details — список {label,value,kind} того, что вписал ИИ (для прозрачности):
    показываем «поле → значение», а черновики мотивации помечаем «проверь»."""
    payload = {
        "platform": platform or "Форма",
        "filled": list(filled or []),
        "missing": list(missing or []),
        "questions": int(questions or 0),
        "ai": [
            {"label": str(d.get("label") or ""), "value": str(d.get("value") or ""),
             "draft": d.get("kind") == "draft"}
            for d in (ai_details or []) if isinstance(d, dict)
        ],
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
                  .ai{border:1px solid #35407a;background:#141a33;color:#c6cdf5}
                  .ai .ai-h{font-weight:800;margin-bottom:5px;display:block}
                  .ai ul{margin:0;padding:0;list-style:none}.ai li{padding:2px 0;font-size:12px}
                  .ai b{color:#e9ecff}.ai .v{color:#a9b3ef;white-space:pre-wrap;overflow-wrap:anywhere}
                  .ai .draft{color:#f5d778}.ai .draft-tag{font-weight:800}
                </style><section class="card" role="status"><div class="head"><span class="mark">◆</span>
                  <div class="title">WexFlow · форма подготовлена<div class="platform"></div></div>
                  <button type="button" aria-label="Закрыть">×</button></div>
                  <div class="row ok"><span class="label">Заполнено:</span> <span class="filled"></span></div>
                  <div class="row ai-run" hidden></div>
                  <div class="row ai" hidden></div>
                  <div class="row questions" hidden></div><div class="row warn missing" hidden></div>
                  <div class="foot">Проверь данные, поставь нужные согласия и отправь анкету сам. WexFlow не нажимает финальную кнопку.</div>
                </section>`;
                root.querySelector('.platform').textContent=data.platform;
                root.querySelector('.filled').textContent=data.filled.length?data.filled.join(', '):'распознанных полей нет';
                const run=window.__wexflowAiRun;
                const runEl=root.querySelector('.ai-run');
                if(run){
                  runEl.hidden=false;
                  if(run.state==='error'){
                    runEl.classList.add('warn');
                    runEl.textContent='ИИ-проверка пропущена: '+(run.detail||'неизвестная ошибка');
                  } else {
                    runEl.classList.add('ok');
                    runEl.textContent='ИИ-проверка завершена · '+run.step+' из '+run.total+' этапов';
                  }
                }
                const ai=root.querySelector('.ai');
                if(data.ai && data.ai.length){
                  ai.hidden=false;
                  const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
                  const items=data.ai.map(x=>x.draft
                    ? `<li class="draft"><span class="draft-tag">⚠ черновик — проверь:</span> <b>${esc(x.label)}</b> → <span class="v">${esc(x.value)}</span></li>`
                    : `<li><b>${esc(x.label)}</b> → <span class="v">${esc(x.value)}</span></li>`).join('');
                  ai.innerHTML=`<span class="ai-h">Заполнил ИИ:</span><ul>${items}</ul>`;
                }
                const q=root.querySelector('.questions'); if(data.questions){q.hidden=false;q.textContent=`Дополнительных вопросов: ${data.questions}`;}
                const m=root.querySelector('.missing'); if(data.missing.length){m.hidden=false;m.textContent=`Осталось заполнить: ${data.missing.join(', ')}`;}
                root.querySelector('button').addEventListener('click',()=>host.remove());
                document.documentElement.appendChild(host);
            }""",
            payload,
        )
    except Exception:
        pass
