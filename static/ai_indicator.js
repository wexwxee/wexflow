/*
 * Постоянный компактный индикатор ресурса ИИ WexFlow.
 * Единый shell-компонент: подключается один раз из base.html и появляется на
 * всех страницах приложения. Ничего не дублируется по шаблонам.
 *
 * - показывает процент активного провайдера (Groq/Qwen или Gemini), цвет по порогам;
 * - клик открывает popover с раздельными карточками Gemini и Groq;
 * - если ключей нет — «Подключить ИИ» ведёт в мастер (/settings/ai);
 * - опрос локального /api/ai/usage раз в 45 c, пауза при скрытой вкладке;
 * - НЕ обращается к внешним провайдерам и не тратит квоту.
 */
(function () {
  "use strict";
  if (window.__wexflowAiIndicator) return;           // единственный экземпляр
  window.__wexflowAiIndicator = true;

  var POLL_MS = 45000;
  var ENDPOINT = "/api/ai/usage";
  var SETTINGS_URL = "/settings/ai";
  var state = { data: null, open: false, timer: null, inflight: false };

  var COLORS = {
    green: "#2ecc71", normal: "#7cc4ff", yellow: "#f5a623",
    red: "#ef5b67", exhausted: "#ef5b67", error: "#c0392b"
  };

  // ---- стили (self-contained, theme-aware через fallback + data-theme) ----- //
  function injectStyle() {
    if (document.getElementById("wf-ai-ind-style")) return;
    var css =
      "#wfAiInd{position:fixed;left:14px;bottom:14px;z-index:940;font-family:Inter,system-ui,sans-serif;touch-action:none}" +
      "#wfAiInd.dragging{opacity:.92;cursor:grabbing}" +
      "#wfAiInd.dragging .wf-ai-chip{transform:none;box-shadow:0 10px 28px rgba(0,0,0,.45)}" +
      "#wfAiInd .wf-ai-grip{width:9px;height:14px;flex:0 0 auto;opacity:.45;cursor:grab;margin-right:-2px}" +
      "#wfAiInd .wf-ai-chip:hover .wf-ai-grip{opacity:.8}" +
      "#wfAiPop.up{bottom:auto;top:44px}" +
      "#wfAiPop.right{left:auto;right:0}" +
      "#wfAiInd .wf-ai-chip{display:flex;align-items:center;gap:8px;border:1px solid var(--border,rgba(255,255,255,.12));" +
        "background:var(--card,var(--bg-soft,#1b1b20));color:var(--fg,#e8e8ea);border-radius:999px;padding:6px 12px 6px 8px;" +
        "cursor:pointer;font:inherit;font-size:12.5px;line-height:1;box-shadow:0 6px 20px rgba(0,0,0,.25);transition:transform .12s}" +
      "#wfAiInd .wf-ai-chip:hover{transform:translateY(-1px)}" +
      "#wfAiInd .wf-ai-chip:focus-visible{outline:2px solid var(--accent,#7cc4ff);outline-offset:2px}" +
      "#wfAiInd .wf-ai-ring{width:22px;height:22px;flex:0 0 auto;transform:rotate(-90deg)}" +
      "#wfAiInd .wf-ai-ring .bg{stroke:var(--border,rgba(255,255,255,.14))}" +
      "#wfAiInd .wf-ai-pct{font-weight:700;font-variant-numeric:tabular-nums}" +
      "#wfAiInd .wf-ai-lbl{color:var(--muted,#9aa0a6);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" +
      "#wfAiInd .wf-ai-dot{width:8px;height:8px;border-radius:50%}" +
      "#wfAiPop{position:absolute;left:0;bottom:44px;width:332px;max-width:calc(100vw - 28px);background:var(--card,#1b1b20);" +
        "color:var(--fg,#e8e8ea);border:1px solid var(--border,rgba(255,255,255,.14));border-radius:16px;padding:0;" +
        "box-shadow:0 18px 48px rgba(0,0,0,.45);display:none;overflow:hidden}" +
      "#wfAiInd.open #wfAiPop{display:block}" +
      "#wfAiPop .wf-ai-head{padding:14px 14px 12px;border-bottom:1px solid var(--border,rgba(255,255,255,.10))}" +
      "#wfAiPop h4{margin:0 0 3px;font-size:13.5px;font-weight:700;display:flex;align-items:center;gap:7px}" +
      "#wfAiPop .wf-ai-sub{color:var(--muted,#9aa0a6);font-size:11.5px;line-height:1.5}" +
      "#wfAiPop .wf-ai-body{padding:12px 14px}" +
      "#wfAiPop .wf-ai-card{border:1px solid var(--border,rgba(255,255,255,.10));border-radius:12px;padding:11px 12px;margin-bottom:9px}" +
      "#wfAiPop .wf-ai-card.on{border-color:var(--wf-c,#2ecc71)}" +
      "#wfAiPop .wf-ai-card .row{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}" +
      "#wfAiPop .wf-ai-card .name{font-weight:700;font-size:12.5px;display:flex;align-items:center;gap:7px}" +
      "#wfAiPop .wf-ai-card .mdl{font-size:11px;color:var(--muted,#9aa0a6);margin-top:2px}" +
      "#wfAiPop .wf-ai-card .mdl b{color:var(--fg,#e8e8ea);font-weight:600}" +
      "#wfAiPop .wf-ai-card .role{font-size:10px;color:var(--muted,#9aa0a6);border:1px solid var(--border,rgba(255,255,255,.14));" +
        "border-radius:999px;padding:2px 8px;white-space:nowrap}" +
      "#wfAiPop .wf-ai-card .role.main{background:var(--accent,#7cc4ff);color:#0b1220;border-color:transparent;font-weight:700}" +
      "#wfAiPop .wf-ai-bar{height:6px;border-radius:6px;background:var(--border,rgba(255,255,255,.12));margin:10px 0 7px;overflow:hidden}" +
      "#wfAiPop .wf-ai-bar>i{display:block;height:100%;border-radius:6px}" +
      "#wfAiPop .wf-ai-meta{font-size:11px;color:var(--muted,#9aa0a6);line-height:1.55}" +
      "#wfAiPop .wf-ai-meta b{color:var(--fg,#e8e8ea);font-weight:600}" +
      "#wfAiPop .wf-ai-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}" +
      "#wfAiPop .wf-ai-actions button,#wfAiPop .wf-ai-actions a{font:inherit;font-size:12px;border-radius:9px;padding:7px 11px;cursor:pointer;" +
        "border:1px solid var(--border,rgba(255,255,255,.16));background:transparent;color:var(--fg,#e8e8ea);text-decoration:none}" +
      "#wfAiPop .wf-ai-actions button:hover,#wfAiPop .wf-ai-actions a:hover{border-color:var(--accent,#7cc4ff)}" +
      "#wfAiPop .wf-ai-actions .primary{background:#2ecc71;color:#08150d;border-color:transparent;font-weight:700}" +
      "#wfAiPop .wf-ai-note{font-size:10.5px;color:var(--muted,#9aa0a6);margin-top:9px;line-height:1.5}" +
      "html[data-theme=light] #wfAiInd .wf-ai-chip{background:#fff;color:#1b1b20;border-color:rgba(0,0,0,.12)}" +
      "html[data-theme=light] #wfAiPop{background:#fff;color:#1b1b20;border-color:rgba(0,0,0,.12)}" +
      "@media (max-width:640px){#wfAiInd{left:10px;bottom:10px}#wfAiInd .wf-ai-lbl{display:none}}";
    var s = document.createElement("style");
    s.id = "wf-ai-ind-style";
    s.textContent = css;
    document.head.appendChild(s);
  }

  function ringSvg() {
    return '<svg class="wf-ai-ring" viewBox="0 0 24 24" aria-hidden="true">' +
      '<circle class="bg" cx="12" cy="12" r="9" fill="none" stroke-width="3"></circle>' +
      '<circle class="fg" cx="12" cy="12" r="9" fill="none" stroke-width="3" stroke-linecap="round" ' +
      'stroke-dasharray="56.5" stroke-dashoffset="56.5"></circle></svg>';
  }

  function el(html) {
    var d = document.createElement("div");
    d.innerHTML = html.trim();
    return d.firstChild;
  }

  var root, chip, pop;

  function mount() {
    injectStyle();
    root = document.createElement("div");
    root.id = "wfAiInd";
    chip = el(
      '<button type="button" class="wf-ai-chip" aria-haspopup="dialog" aria-expanded="false" ' +
      'aria-label="Ресурс ИИ. Можно перетащить в любое место окна">' +
      '<svg class="wf-ai-grip" viewBox="0 0 6 14" aria-hidden="true">' +
      '<circle cx="1.5" cy="2" r="1.2" fill="currentColor"></circle>' +
      '<circle cx="4.5" cy="2" r="1.2" fill="currentColor"></circle>' +
      '<circle cx="1.5" cy="7" r="1.2" fill="currentColor"></circle>' +
      '<circle cx="4.5" cy="7" r="1.2" fill="currentColor"></circle>' +
      '<circle cx="1.5" cy="12" r="1.2" fill="currentColor"></circle>' +
      '<circle cx="4.5" cy="12" r="1.2" fill="currentColor"></circle></svg>' +
      ringSvg() +
      '<span class="wf-ai-pct">—</span><span class="wf-ai-lbl">ИИ</span></button>'
    );
    pop = document.createElement("div");
    pop.id = "wfAiPop";
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", "Ресурс ИИ по провайдерам");
    root.appendChild(chip);
    root.appendChild(pop);
    document.body.appendChild(root);

    chip.addEventListener("click", function (e) {
      e.stopPropagation();
      if (drag.moved) { drag.moved = false; return; }   // это было перетаскивание, не клик
      toggle();
    });
    document.addEventListener("click", function (e) {
      if (state.open && !root.contains(e.target)) close();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && state.open) { close(); chip.focus(); }
    });
    installDrag();
    restorePosition();
    window.addEventListener("resize", function () { clampToViewport(); });
  }

  // ── перетаскивание: индикатор можно поставить в любой угол окна ─────────── //
  var POS_KEY = "wf-ai-indicator-pos";
  var drag = { active: false, moved: false, dx: 0, dy: 0 };

  function applyPosition(left, top) {
    root.style.left = Math.round(left) + "px";
    root.style.top = Math.round(top) + "px";
    root.style.right = "auto";
    root.style.bottom = "auto";
    orientPopover();
  }

  function clampToViewport() {
    var rect = root.getBoundingClientRect();
    if (!rect.width) return;
    if (root.style.top === "" && root.style.left === "") return;   // позиция по умолчанию
    var maxLeft = Math.max(4, window.innerWidth - rect.width - 4);
    var maxTop = Math.max(4, window.innerHeight - rect.height - 4);
    applyPosition(Math.min(Math.max(4, rect.left), maxLeft),
                  Math.min(Math.max(4, rect.top), maxTop));
  }

  function savePosition() {
    try {
      var rect = root.getBoundingClientRect();
      localStorage.setItem(POS_KEY, JSON.stringify({ left: rect.left, top: rect.top }));
    } catch (e) {}
  }

  function restorePosition() {
    try {
      var saved = JSON.parse(localStorage.getItem(POS_KEY) || "null");
      if (!saved || typeof saved.left !== "number") return;
      applyPosition(saved.left, saved.top);
      clampToViewport();
    } catch (e) {}
  }

  /** Popover всегда раскрывается внутрь экрана, куда бы ни перетащили индикатор. */
  function orientPopover() {
    var rect = root.getBoundingClientRect();
    pop.classList.toggle("up", rect.top < window.innerHeight / 2);
    pop.classList.toggle("right", rect.left > window.innerWidth / 2);
  }

  function installDrag() {
    chip.addEventListener("pointerdown", function (e) {
      if (e.button !== 0) return;
      var rect = root.getBoundingClientRect();
      drag.active = true;
      drag.moved = false;
      drag.dx = e.clientX - rect.left;
      drag.dy = e.clientY - rect.top;
      try { chip.setPointerCapture(e.pointerId); } catch (_) {}
    });
    chip.addEventListener("pointermove", function (e) {
      if (!drag.active) return;
      var left = e.clientX - drag.dx, top = e.clientY - drag.dy;
      if (!drag.moved) {
        // маленькое дрожание пальцем/мышью не должно превращать клик в перенос
        if (Math.abs(e.clientX - (drag.dx + root.getBoundingClientRect().left)) < 4 &&
            Math.abs(e.clientY - (drag.dy + root.getBoundingClientRect().top)) < 4) return;
        drag.moved = true;
        root.classList.add("dragging");
        close();
      }
      var rect = root.getBoundingClientRect();
      var maxLeft = Math.max(4, window.innerWidth - rect.width - 4);
      var maxTop = Math.max(4, window.innerHeight - rect.height - 4);
      applyPosition(Math.min(Math.max(4, left), maxLeft), Math.min(Math.max(4, top), maxTop));
    });
    function end(e) {
      if (!drag.active) return;
      drag.active = false;
      root.classList.remove("dragging");
      if (drag.moved) savePosition();
      try { chip.releasePointerCapture(e.pointerId); } catch (_) {}
    }
    chip.addEventListener("pointerup", end);
    chip.addEventListener("pointercancel", end);
  }

  function toggle() { state.open ? close() : openPop(); }
  function openPop() {
    state.open = true; root.classList.add("open");
    chip.setAttribute("aria-expanded", "true");
    orientPopover();
    renderPop();
  }
  function close() { state.open = false; root.classList.remove("open"); chip.setAttribute("aria-expanded", "false"); }

  // ------------------------------ отрисовка -------------------------------- //
  function providerLabel(p) {
    if (p === "groq") return "Groq";
    if (p === "gemini") return "Gemini";
    return "ИИ";
  }
  function modelShort(m) {
    if (!m) return "";
    if (m.indexOf("qwen") >= 0) return "Qwen 3.6";
    if (m.indexOf("gpt-oss") >= 0) return "GPT-OSS 120B";
    if (m.indexOf("gemini") >= 0) return "Gemini " + (m.indexOf("lite") >= 0 ? "2.5 Flash Lite" : "2.5 Flash");
    return m;
  }
  // Модель по умолчанию для НЕподключённого провайдера — чтобы человек сразу видел,
  // что Qwen не отдельный провайдер, а модель внутри Groq.
  function defaultModel(p) { return p === "groq" ? "Qwen 3.6" : "Gemini 2.5 Flash"; }

  function renderChip() {
    var ai = (state.data && state.data.ai) || {};
    var pctEl = chip.querySelector(".wf-ai-pct");
    var lblEl = chip.querySelector(".wf-ai-lbl");
    var ringFg = chip.querySelector(".wf-ai-ring .fg");
    if (!ai.connected || !ai.compact) {
      pctEl.textContent = "+";
      lblEl.textContent = "Подключить ИИ";
      chip.title = "ИИ не подключён — нажми, чтобы подключить бесплатно";
      if (ringFg) { ringFg.style.stroke = "var(--muted,#9aa0a6)"; ringFg.style.strokeDashoffset = "56.5"; }
      chip.setAttribute("aria-label", "ИИ не подключён");
      return;
    }
    var c = ai.compact;
    var pct = Math.max(0, Math.min(100, c.percent_remaining | 0));
    var color = COLORS[c.color] || COLORS.green;
    pctEl.textContent = pct + "%";
    pctEl.style.color = color;
    lblEl.textContent = providerLabel(c.provider) + " · " + modelShort(c.model);
    if (ringFg) {
      ringFg.style.stroke = color;
      ringFg.style.strokeDashoffset = String(56.5 * (1 - pct / 100));
    }
    var est = c.estimate ? " (оценка)" : "";
    chip.title = providerLabel(c.provider) + " · " + modelShort(c.model) + " — осталось " + pct + "%" + est;
    chip.setAttribute("aria-label", "Ресурс ИИ: " + providerLabel(c.provider) + ", осталось " + pct + " процентов");
  }

  function cardHtml(name, card, activeProvider) {
    var head = function (right, dotColor) {
      return '<div class="row"><div><div class="name">' +
        '<span class="wf-ai-dot" style="background:' + (dotColor || "var(--border,rgba(255,255,255,.2))") + '"></span>' +
        providerLabel(name) + '</div>' +
        '<div class="mdl">модель <b>' +
        (card && card.connected ? (modelShort(card.model) || defaultModel(name)) : defaultModel(name)) +
        '</b></div></div>' + right + '</div>';
    };

    if (!card || !card.connected) {
      var pitch = name === "groq"
        ? "Бесплатно, без карты — запускает Qwen 3.6."
        : "Ключ Google AI Studio, тоже бесплатный.";
      return '<div class="wf-ai-card">' + head('<span class="role">не подключён</span>') +
        '<div class="wf-ai-meta" style="margin-top:8px">' + pitch + '</div></div>';
    }

    var u = card.usage || {};
    var pct = Math.max(0, Math.min(100, (u.percent_remaining | 0)));
    var color = COLORS[(u.color)] || COLORS.green;
    var role = card.role === "primary"
      ? '<span class="role main">основной</span>'
      : (card.role === "secondary" ? '<span class="role">резервный</span>' : '');
    var req = u.requests || {};
    var meta;
    if (req.precise) {
      meta = "Запросы за день: <b>" + req.remaining + "</b> из " + req.limit + " — точно";
    } else {
      meta = "Запросов сделано: <b>" + (req.used | 0) + "</b> — лимит уточнится после ответа провайдера";
    }
    if (u.tokens_minute) {
      meta += "<br>Токены в минуту: <b>" + u.tokens_minute.remaining + "</b> из " + u.tokens_minute.limit;
    }
    var tl = u.tokens_day_local || {};
    if (tl.total) meta += "<br>Токены за день: ~" + tl.total + " (оценка)";
    if (u.limiting === "tokens_day") meta += "<br><b>Ограничивают дневные токены</b>";
    if (u.reset_at) meta += "<br>Сброс: " + fmtReset(u.reset_at);
    if (u.last_error_code) meta += "<br>Ошибка: " + safeErr(u.last_error_code);

    return '<div class="wf-ai-card on" style="--wf-c:' + color + '">' + head(role, color) +
      '<div class="wf-ai-bar"><i style="width:' + pct + '%;background:' + color + '"></i></div>' +
      '<div class="wf-ai-meta"><b style="font-size:12.5px">' + pct + '%</b> ресурса осталось<br>' +
      meta + '</div></div>';
  }

  function renderPop() {
    var ai = (state.data && state.data.ai) || {};
    var providers = ai.providers || {};
    var active = ai.primary || "";
    var head, sub, dot = "";
    if (ai.connected && ai.active) {
      var c = ai.compact || {};
      dot = '<span class="wf-ai-dot" style="background:' + (COLORS[c.color] || COLORS.green) + '"></span>';
      head = providerLabel(ai.active.provider) + " · " + modelShort(ai.active.model);
      sub = (active === "gemini"
        ? "Сейчас отвечает Gemini. Groq подключён резервом — включится только при исчерпании дневного лимита или сбое."
        : "Сейчас отвечает Groq на модели " + modelShort(ai.active.model) + ".");
    } else {
      head = "ИИ не подключён";
      sub = "Подключи бесплатный ключ — пара минут, карта не нужна. Groq запускает модель Qwen 3.6.";
    }
    var html = '<div class="wf-ai-head"><h4>' + dot + head + '</h4>' +
               '<div class="wf-ai-sub">' + sub + '</div></div><div class="wf-ai-body">';
    html += cardHtml("groq", providers.groq, active);
    html += cardHtml("gemini", providers.gemini, active);
    html += '<div class="wf-ai-actions">' +
      '<a class="primary" href="' + SETTINGS_URL + '">' + (ai.connected ? "Настроить ИИ" : "Подключить ИИ") + '</a>' +
      '<a href="' + SETTINGS_URL + '#stats">Статистика</a>' +
      (ai.connected ? '<button type="button" id="wfAiCheck">Проверить</button>' : '') +
      '</div>';
    html += '<div class="wf-ai-note">Провайдер — сервис с ключом, модель — сам ИИ внутри него. ' +
            'Ключи хранятся только на этом компьютере, зашифрованно, и не уходят в облако.</div></div>';
    pop.innerHTML = html;
    var check = pop.querySelector("#wfAiCheck");
    if (check) check.addEventListener("click", doCheck);
  }

  function doCheck(e) {
    var btn = e.currentTarget;
    var ai = (state.data && state.data.ai) || {};
    var provider = ai.primary;
    if (!provider) return;
    btn.disabled = true; btn.textContent = "Проверяю…";
    fetch("/api/ai/validate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: provider })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.usage) { state.data.ai = d.usage; renderChip(); }
      btn.textContent = d && d.ok ? "Подключение в порядке ✓" : "Проблема: " + safeErr(d && d.error_code);
      renderPopSoon();
    }).catch(function () { btn.textContent = "Не удалось проверить"; })
      .finally(function () { setTimeout(function () { btn.disabled = false; }, 400); });
  }
  function renderPopSoon() { setTimeout(function () { if (state.open) renderPop(); }, 1200); }

  function safeErr(code) {
    var map = {
      invalid_key: "ключ не принят", permission_denied: "нет доступа к модели",
      rate_limit_rpm: "минутный лимит запросов", rate_limit_rpd: "дневной лимит запросов",
      rate_limit_tpm: "минутный лимит токенов", rate_limit_tpd: "дневной лимит токенов",
      provider_timeout: "таймаут", provider_unavailable: "провайдер недоступен",
      model_not_found: "модель недоступна", invalid_request: "некорректный запрос",
      offline: "нет связи", not_connected: "не подключён"
    };
    return map[code] || (code || "ошибка");
  }
  function fmtReset(ts) {
    try {
      var d = new Date(ts * 1000);
      return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    } catch (e) { return ""; }
  }

  // ------------------------------ данные ----------------------------------- //
  // force=true — первичная загрузка/явный запрос: выполняется даже если вкладка
  // сейчас скрыта, иначе индикатор навсегда остался бы пустым. Периодический
  // опрос при document.hidden всё равно приостановлен (см. startPolling).
  function refresh(force) {
    if (state.inflight || (document.hidden && force !== true)) return;
    state.inflight = true;
    fetch(ENDPOINT, { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (d) { state.data = d; renderChip(); if (state.open) renderPop(); })
      .catch(function () {})
      .finally(function () { state.inflight = false; });
  }

  function startPolling() {
    if (state.timer) return;
    state.timer = setInterval(function () { if (!document.hidden) refresh(); }, POLL_MS);
  }

  function forceRefresh() { refresh(true); }

  function init() {
    mount();
    forceRefresh();                                   // первичный показ — всегда
    startPolling();
    document.addEventListener("visibilitychange", function () { if (!document.hidden) refresh(); });
    window.addEventListener("focus", function () { refresh(); });
    // другие части приложения могут попросить немедленное обновление после ИИ-вызова
    window.addEventListener("wexflow:ai-used", forceRefresh);
    window.refreshAiIndicator = forceRefresh;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
