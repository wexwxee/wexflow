/* Панель-помощник (этап 4).

   Помощник ничего не сочиняет: он показывает то, что вернуло приложение.
   Пока без ИИ — просьбу разбирают словари на сервере, и это сознательно:
   помощник должен работать у человека без ключа и с исчерпанной квотой.

   Панель монтируется в штатный оверлей (#wf-overlay-root), поэтому её можно
   добавить на любую страницу, не трогая вёрстку. */
(function () {
  "use strict";

  var STORAGE_KEY = "wf-ask-open";
  var panel = null;
  var body = null;
  var input = null;
  var state = { ai: false, hints: [] };

  function root() {
    return (window.WexFlowOverlayRoot && window.WexFlowOverlayRoot()) || document.body;
  }

  function jobId() {
    var match = String(location.pathname || "").match(/^\/job\/([^/?#]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function scrollDown() {
    if (body) body.scrollTop = body.scrollHeight;
  }

  function say(text, cls) {
    if (!text) return;
    var node = el("div", "wf-ask-msg " + (cls || "bot"), text);
    body.appendChild(node);
    scrollDown();
    return node;
  }

  function card(item) {
    var link = el("a", "wf-ask-card");
    link.href = item.href || "#";
    link.appendChild(el("b", null, item.title || ""));
    var parts = [];
    if (item.brand) parts.push(item.brand);
    if (item.city) parts.push(item.city);
    if (item.hours) parts.push(item.hours);   // подпись готовит сервер
    if (item.subtitle) parts.push(item.subtitle);
    if (parts.length) link.appendChild(el("span", null, parts.join(" · ")));
    if (item.why && item.why.length) {
      link.appendChild(el("span", "wf-ask-why", item.why.join(" · ")));
    }
    return link;
  }

  // Хвост диалога: без него сервер читает каждое сообщение как первое, и
  // «мне 20 лет» после «что есть рядом» теряет всякий смысл. Держим только
  // текст — карточки вакансий модели пересказывать незачем.
  var history = [];

  function remember(role, text) {
    if (!text) return;
    history.push({ role: role, text: String(text).slice(0, 200) });
    if (history.length > 6) history = history.slice(-6);
  }

  function render(data) {
    if (data.reply) { say(data.reply, "bot"); remember("bot", data.reply); }
    var list = data.results || [];
    list.forEach(function (item) { body.appendChild(card(item)); });
    // Подсказку «ничего не нашлось» показываем только когда текст ответа
    // написало приложение. Если формулировал ИИ, он уже сказал то же самое
    // своими словами, и вторая строка выглядела как заедание.
    if (!list.length && data.kind === "jobs" && data.empty_hint && !data.ai_wording) {
      say(data.empty_hint, "bot");
    }
    if (data.kind === "confirm" || (data.href && data.button)) {
      var action = el("a", "wf-ask-card");
      action.href = data.href;
      action.appendChild(el("b", null, data.button || "Открыть"));
      body.appendChild(action);
    }
    if (data.tool_human) {
      body.appendChild(el("div", "wf-ask-tool", "сделано инструментом: " + data.tool_human));
    }
    if (data.understood) {
      body.appendChild(el("div", "wf-ask-tool", "понял так: " + data.understood));
    }
    // Помощник смотрит всю ленту, а на странице могут стоять свои фильтры.
    // Без этой строки его находки выглядят как «в списке этого нет».
    if (data.scope_note && list.length) {
      body.appendChild(el("div", "wf-ask-tool", data.scope_note));
    }
    scrollDown();
  }

  function chips() {
    var box = el("div", "wf-ask-chips");
    (state.hints || []).forEach(function (hint) {
      var chip = el("button", "wf-ask-chip", hint);
      chip.type = "button";
      chip.addEventListener("click", function () { ask(hint); });
      box.appendChild(chip);
    });
    body.appendChild(box);
  }

  function ask(text) {
    if (!text) return;
    say(text, "me");
    var sent = history.slice();          // без только что заданного вопроса
    remember("me", text);
    if (input) input.value = "";
    var pending = say("Смотрю…", "bot");
    fetch("/api/assistant/ask", {
      method: "POST",
      headers: { "content-type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ text: text, job_id: jobId(), history: sent })
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (pending) pending.remove();
        render(data || {});
      })
      .catch(function () {
        if (pending) pending.textContent = "Не получилось ответить. Попробуй ещё раз.";
      });
  }

  function build() {
    panel = el("aside", null);
    panel.id = "wfAskPanel";
    panel.hidden = true;
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Помощник");

    var head = el("div", "wf-ask-head");
    var title = el("div");
    title.appendChild(el("b", null, "Помощник"));
    title.appendChild(el("span", "wf-ask-sub",
      "Ищет по твоей базе и объясняет. Заявку не отправляет — кнопку жмёшь ты."));
    head.appendChild(title);
    var close = el("button", "wf-ask-close", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Закрыть помощника");
    close.addEventListener("click", function () { toggle(false); });
    head.appendChild(close);

    body = el("div", "wf-ask-body");

    var foot = el("div", "wf-ask-foot");
    var form = el("form");
    input = el("input");
    input.type = "text";
    input.placeholder = "нетто херлев 15 часов";
    input.setAttribute("aria-label", "Спросить помощника");
    var send = el("button", null, "Спросить");
    send.type = "submit";
    form.appendChild(input);
    form.appendChild(send);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      ask((input.value || "").trim());
    });
    foot.appendChild(form);

    panel.appendChild(head);
    panel.appendChild(body);
    panel.appendChild(foot);
    root().appendChild(panel);

    say("Привет! Скажи, что ищешь — я поищу по твоей базе вакансий.", "bot");
    chips();
  }

  function toggle(open) {
    if (!panel) build();
    panel.hidden = !open;
    document.documentElement.classList.toggle("wf-ask-open", !!open);
    try { localStorage.setItem(STORAGE_KEY, open ? "1" : "0"); } catch (e) { /* приват-режим */ }
    if (open && input) input.focus();
  }

  function mountButton() {
    var button = el("button", null);
    button.id = "wfAskBtn";
    button.type = "button";
    button.textContent = "✦ Помощник";
    button.title = "Помощник: найдёт вакансии, объяснит вердикт, покажет, что рядом";
    button.addEventListener("click", function () {
      toggle(panel ? panel.hidden : true);
    });
    root().appendChild(button);
  }

  function start() {
    mountButton();
    fetch("/api/assistant/state", { credentials: "include" })
      .then(function (r) { return r.json(); })
      .then(function (data) { state = data || state; })
      .catch(function () { /* панель полезна и без подсказок */ })
      .then(function () {
        var open = false;
        try { open = localStorage.getItem(STORAGE_KEY) === "1"; } catch (e) { open = false; }
        if (open) toggle(true);
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
