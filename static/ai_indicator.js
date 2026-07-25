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
      "#wfAiInd{position:fixed;left:14px;bottom:14px;z-index:940;font-family:Inter,system-ui,sans-serif}" +
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
      "#wfAiPop{position:absolute;left:0;bottom:44px;width:320px;max-width:calc(100vw - 28px);background:var(--card,#1b1b20);" +
        "color:var(--fg,#e8e8ea);border:1px solid var(--border,rgba(255,255,255,.14));border-radius:14px;padding:14px;" +
        "box-shadow:0 18px 48px rgba(0,0,0,.42);display:none}" +
      "#wfAiInd.open #wfAiPop{display:block}" +
      "#wfAiPop h4{margin:0 0 2px;font-size:13px;font-weight:700}" +
      "#wfAiPop .wf-ai-sub{color:var(--muted,#9aa0a6);font-size:11.5px;margin-bottom:10px}" +
      "#wfAiPop .wf-ai-card{border:1px solid var(--border,rgba(255,255,255,.10));border-radius:11px;padding:10px;margin-bottom:8px}" +
      "#wfAiPop .wf-ai-card .row{display:flex;align-items:center;justify-content:space-between;gap:8px}" +
      "#wfAiPop .wf-ai-card .name{font-weight:600;font-size:12.5px;display:flex;align-items:center;gap:6px}" +
      "#wfAiPop .wf-ai-card .role{font-size:10px;color:var(--muted,#9aa0a6);border:1px solid var(--border,rgba(255,255,255,.14));" +
        "border-radius:999px;padding:1px 7px}" +
      "#wfAiPop .wf-ai-bar{height:6px;border-radius:6px;background:var(--border,rgba(255,255,255,.12));margin:8px 0 6px;overflow:hidden}" +
      "#wfAiPop .wf-ai-bar>i{display:block;height:100%;border-radius:6px}" +
      "#wfAiPop .wf-ai-meta{font-size:11px;color:var(--muted,#9aa0a6);line-height:1.5}" +
      "#wfAiPop .wf-ai-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}" +
      "#wfAiPop .wf-ai-actions button,#wfAiPop .wf-ai-actions a{font:inherit;font-size:12px;border-radius:8px;padding:6px 10px;cursor:pointer;" +
        "border:1px solid var(--border,rgba(255,255,255,.16));background:transparent;color:var(--fg,#e8e8ea);text-decoration:none}" +
      "#wfAiPop .wf-ai-actions .primary{background:var(--accent,#7cc4ff);color:#0b1220;border-color:transparent;font-weight:600}" +
      "#wfAiPop .wf-ai-note{font-size:11px;color:var(--muted,#9aa0a6);margin-top:8px}" +
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
      'aria-label="Ресурс ИИ">' + ringSvg() +
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
      toggle();
    });
    document.addEventListener("click", function (e) {
      if (state.open && !root.contains(e.target)) close();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && state.open) { close(); chip.focus(); }
    });
  }

  function toggle() { state.open ? close() : openPop(); }
  function openPop() { state.open = true; root.classList.add("open"); chip.setAttribute("aria-expanded", "true"); renderPop(); }
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
    if (m.indexOf("gpt-oss") >= 0) return "GPT-OSS";
    if (m.indexOf("gemini") >= 0) return "Gemini";
    return m;
  }

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
    if (!card || !card.connected) {
      var isGroq = name === "groq";
      return '<div class="wf-ai-card"><div class="row"><span class="name">' + providerLabel(name) +
        '</span><span class="role">не подключён</span></div>' +
        (isGroq ? '<div class="wf-ai-meta">Бесплатный личный ключ Qwen — подключается за пару минут, без карты.</div>'
                : '<div class="wf-ai-meta">Необязателен для обычного пользователя.</div>') + '</div>';
    }
    var u = card.usage || {};
    var pct = Math.max(0, Math.min(100, (u.percent_remaining | 0)));
    var color = COLORS[(u.color)] || COLORS.green;
    var role = card.role === "primary" ? "основной" : (card.role === "secondary" ? "резервный" : "");
    var req = u.requests || {};
    var precise = req.precise ? "точно" : "оценка";
    var meta = "Запросы: " + (req.remaining != null ? req.remaining : "—") + " из " + (req.limit || "—") +
      " (" + precise + ")";
    if (u.tokens_minute) {
      meta += "<br>Токены/мин: " + u.tokens_minute.remaining + " из " + u.tokens_minute.limit;
    }
    var tl = u.tokens_day_local || {};
    if (tl.total) meta += "<br>Токены за день (лок.): ~" + tl.total;
    if (u.limiting === "tokens_day") meta += "<br><b>Ограничивают дневные токены</b>";
    if (u.reset_at) meta += "<br>Сброс: " + fmtReset(u.reset_at);
    if (u.last_error_code) meta += "<br>Последняя ошибка: " + safeErr(u.last_error_code);
    return '<div class="wf-ai-card"><div class="row"><span class="name">' +
      '<span class="wf-ai-dot" style="background:' + color + '"></span>' + providerLabel(name) +
      ' · ' + modelShort(card.model) + '</span>' +
      (role ? '<span class="role">' + role + '</span>' : '') + '</div>' +
      '<div class="wf-ai-bar"><i style="width:' + pct + '%;background:' + color + '"></i></div>' +
      '<div class="wf-ai-meta">Осталось ' + pct + '%<br>' + meta + '</div></div>';
  }

  function renderPop() {
    var ai = (state.data && state.data.ai) || {};
    var providers = ai.providers || {};
    var active = ai.primary || "";
    var head, sub;
    if (ai.connected && ai.active) {
      head = "Активный ИИ: " + providerLabel(ai.active.provider) + " · " + modelShort(ai.active.model);
      sub = active === "gemini" ? "Основной — Gemini, резерв — Groq (если подключён)."
                                : "Основной провайдер — Groq (Qwen).";
    } else {
      head = "ИИ не подключён";
      sub = "Подключи бесплатный ключ Groq — займёт пару минут, карта не нужна.";
    }
    var html = '<h4>' + head + '</h4><div class="wf-ai-sub">' + sub + '</div>';
    html += cardHtml("gemini", providers.gemini, active);
    html += cardHtml("groq", providers.groq, active);
    html += '<div class="wf-ai-actions">' +
      '<a class="primary" href="' + SETTINGS_URL + '">' + (ai.connected ? "Настроить ИИ" : "Подключить ИИ") + '</a>' +
      '<a href="' + SETTINGS_URL + '#stats">Подробная статистика</a>' +
      (ai.connected ? '<button type="button" id="wfAiCheck">Проверить подключение</button>' : '') +
      '</div>';
    html += '<div class="wf-ai-note">Ключи хранятся только на этом компьютере, зашифрованно, и не уходят в облако.</div>';
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
