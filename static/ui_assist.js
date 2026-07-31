(function () {
  "use strict";

  var openSelect = null;
  var portal = null;
  var selectUid = 0;

  function overlayRoot() {
    return window.WexFlowOverlayRoot
      ? window.WexFlowOverlayRoot()
      : document.documentElement;
  }

  function optionRows(select) {
    return Array.from(select.options).map(function (option, index) {
      return {
        index: index,
        value: option.value,
        label: option.textContent.trim(),
        disabled: option.disabled,
        recommended: option.dataset.recommended === "true",
        description: option.dataset.description || ""
      };
    });
  }

  function closeSelect() {
    if (portal) portal.remove();
    portal = null;
    if (openSelect) {
      openSelect.wrapper.classList.remove("open");
      openSelect.button.setAttribute("aria-expanded", "false");
    }
    openSelect = null;
  }

  function positionPortal(button) {
    if (!portal) return;
    var rect = button.getBoundingClientRect();
    var margin = 8;
    var gap = 5;
    var scale = Math.max(
      0.8,
      Math.min(1.5, Number(document.documentElement.dataset.uiZoom || 100) / 100)
    );
    portal.style.setProperty("--wf-overlay-scale", String(scale));
    var width = Math.min(Math.max(rect.width, 240), window.innerWidth - margin * 2);
    portal.style.width = width + "px";
    portal.style.left = Math.max(
      margin,
      Math.min(rect.left, window.innerWidth - width - margin)
    ) + "px";

    var list = portal.querySelector(".wf-select-list");
    if (list) list.style.maxHeight = "";
    var roomBelow = Math.max(0, window.innerHeight - rect.bottom - gap - margin);
    var roomAbove = Math.max(0, rect.top - gap - margin);
    var height = portal.getBoundingClientRect().height;
    var below = height <= roomBelow || (height > roomAbove && roomBelow >= roomAbove);
    var available = below ? roomBelow : roomAbove;
    if (list && height > available) {
      var listHeight = list.getBoundingClientRect().height;
      list.style.maxHeight = Math.max(96, listHeight - (height - available)) + "px";
      height = portal.getBoundingClientRect().height;
    }
    portal.style.top = (
      below
        ? Math.min(window.innerHeight - height - margin, rect.bottom + gap)
        : Math.max(margin, rect.top - height - gap)
    ) + "px";
  }

  function updateSelect(api) {
    var option = api.select.options[api.select.selectedIndex];
    api.value.textContent = option ? option.textContent.trim() : "Не выбрано";
    api.wrapper.classList.toggle("has-value", Boolean(api.select.value));
    api.button.disabled = api.select.disabled;
  }

  function choose(api, index) {
    var option = api.select.options[index];
    if (!option || option.disabled) return;
    api.select.selectedIndex = index;
    api.select.dispatchEvent(new Event("input", { bubbles: true }));
    api.select.dispatchEvent(new Event("change", { bubbles: true }));
    updateSelect(api);
    closeSelect();
    api.button.focus();
  }

  function showSelect(api) {
    if (openSelect === api) { closeSelect(); return; }
    closeSelect();
    openSelect = api;
    api.wrapper.classList.add("open");
    api.button.setAttribute("aria-expanded", "true");
    portal = document.createElement("div");
    portal.className = "wf-select-portal";
    portal.dataset.owner = api.id;
    var rows = optionRows(api.select);
    var searchable = api.select.dataset.searchable === "true" || rows.length > 9;
    var search = null;
    if (searchable) {
      search = document.createElement("input");
      search.className = "wf-select-search";
      search.type = "search";
      search.placeholder = api.select.dataset.searchPlaceholder || "Начни вводить для поиска…";
      search.setAttribute("aria-label", "Поиск по вариантам");
      portal.appendChild(search);
    }
    var list = document.createElement("div");
    list.className = "wf-select-list";
    list.setAttribute("role", "listbox");
    portal.appendChild(list);
    overlayRoot().appendChild(portal);

    function render(query) {
      var folded = String(query || "").trim().toLocaleLowerCase();
      list.innerHTML = "";
      var shown = rows.filter(function (row) {
        return !folded || (row.label + " " + row.description).toLocaleLowerCase().includes(folded);
      });
      if (!shown.length) {
        var empty = document.createElement("div");
        empty.className = "wf-select-empty";
        empty.textContent = "Ничего не найдено";
        list.appendChild(empty);
        return;
      }
      shown.forEach(function (row) {
        var item = document.createElement("button");
        item.type = "button";
        item.className = "wf-select-option";
        item.dataset.optionIndex = String(row.index);
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", String(row.index === api.select.selectedIndex));
        if (row.index === api.select.selectedIndex) item.classList.add("selected");
        item.disabled = row.disabled;
        var label = document.createElement("span");
        label.textContent = row.label;
        item.appendChild(label);
        if (row.recommended) {
          var tag = document.createElement("span");
          tag.className = "wf-select-recommended";
          tag.textContent = "рекомендуем";
          item.appendChild(tag);
        }
        if (row.description) {
          var description = document.createElement("small");
          description.textContent = row.description;
          item.appendChild(description);
        }
        item.addEventListener("click", function () { choose(api, row.index); });
        list.appendChild(item);
      });
      positionPortal(api.button);
    }
    render("");
    if (search) {
      search.addEventListener("input", function () { render(search.value); });
      setTimeout(function () { search.focus(); }, 0);
    } else {
      var selected = list.querySelector(".selected");
      if (selected) selected.scrollIntoView({ block: "nearest" });
    }
    positionPortal(api.button);
  }

  function enhanceSelect(select) {
    if (!select || select.dataset.wfEnhanced || select.multiple || Number(select.size) > 1) return;
    select.dataset.wfEnhanced = "true";
    select.classList.add("wf-native-select");
    var wrapper = document.createElement("div");
    wrapper.className = "wf-select";
    var button = document.createElement("button");
    button.type = "button";
    button.className = "wf-select-button";
    button.setAttribute("aria-haspopup", "listbox");
    button.setAttribute("aria-expanded", "false");
    var id = "wf-select-" + (++selectUid);
    button.id = id;
    var value = document.createElement("span");
    value.className = "wf-select-value";
    var chevron = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    chevron.setAttribute("viewBox", "0 0 24 24");
    chevron.setAttribute("aria-hidden", "true");
    chevron.classList.add("wf-select-chevron");
    chevron.innerHTML = '<path d="m6 9 6 6 6-6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>';
    button.append(value, chevron);
    select.insertAdjacentElement("afterend", wrapper);
    wrapper.appendChild(button);
    var api = { id: id, select: select, wrapper: wrapper, button: button, value: value };
    select._wfSelect = api;
    updateSelect(api);
    button.addEventListener("click", function () { if (!select.disabled) showSelect(api); });
    button.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        showSelect(api);
      }
    });
    select.addEventListener("change", function () { updateSelect(api); });
    select.addEventListener("wf:refresh", function () { updateSelect(api); });
    new MutationObserver(function () { updateSelect(api); }).observe(select, {
      childList: true, subtree: true, attributes: true, attributeFilter: ["selected", "disabled"]
    });
  }

  function enhanceAll(root) {
    if (root.matches && root.matches("select")) enhanceSelect(root);
    if (root.querySelectorAll) root.querySelectorAll("select").forEach(enhanceSelect);
  }

  document.addEventListener("click", function (event) {
    if (openSelect && !openSelect.wrapper.contains(event.target) && !(portal && portal.contains(event.target))) closeSelect();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeSelect();
  });
  window.addEventListener("resize", function () { if (openSelect) positionPortal(openSelect.button); });
  document.addEventListener("scroll", function () { if (openSelect) positionPortal(openSelect.button); }, true);

  var activeField = null;
  var modal = null;

  function detectedSource(text) {
    return /[іїєґІЇЄҐ]/.test(text || "") ? "uk" : "ru";
  }

  function recommendedTarget(field) {
    return field.dataset.recommendedLanguage === "en" ? "en" : "da";
  }

  function createModal() {
    if (modal) return modal;
    modal = document.createElement("div");
    modal.className = "wf-ai-modal";
    modal.hidden = true;
    modal.innerHTML =
      '<section class="wf-ai-dialog" role="dialog" aria-modal="true" aria-labelledby="wf-ai-title">' +
        '<div class="wf-ai-head"><div class="wf-ai-head-copy"><h2 id="wf-ai-title">Перевести и проверить текст</h2>' +
        '<p>ИИ покажет результат до замены. Новые факты, навыки и опыт добавлять запрещено.</p></div>' +
        '<button type="button" class="wf-ai-close" data-ai-close aria-label="Закрыть">×</button></div>' +
        '<div class="wf-ai-grid">' +
          '<div class="wf-ai-control"><label>Исходный язык</label><select data-ai-source>' +
            '<option value="ru">Русский</option><option value="uk">Украинский</option></select></div>' +
          '<div class="wf-ai-control"><label>Язык ответа <span class="wf-lang-recommend" data-ai-target-note>датский рекомендуем</span></label><select data-ai-target>' +
            '<option value="da" data-recommended="true" data-description="Лучше совпадает с датской анкетой">Датский</option>' +
            '<option value="en" data-description="Выбирай для англоязычной анкеты или если не можешь проверить датский">Английский</option></select></div>' +
          '<div class="wf-ai-control"><label>Что сделать</label><select data-ai-mode>' +
            '<option value="translate">Только перевести</option>' +
            '<option value="correct">Перевести и исправить ошибки</option>' +
            '<option value="polish">Перевести и слегка улучшить</option></select></div>' +
        '</div>' +
        '<div class="wf-ai-preview"><label>Предложение ИИ</label><textarea data-ai-result readonly placeholder="Здесь появится результат"></textarea></div>' +
        '<div class="wf-ai-explanation" data-ai-explanation hidden></div>' +
        '<div class="wf-ai-error" data-ai-error hidden></div>' +
        '<div class="wf-ai-actions">' +
          '<button type="button" class="wf-ai-action primary" data-ai-run>Получить вариант</button>' +
          '<button type="button" class="wf-ai-action primary" data-ai-accept hidden>Применить к полю</button>' +
          '<button type="button" class="wf-ai-action" data-ai-close>Оставить исходный</button>' +
          '<span class="wf-ai-privacy">Текст уйдёт подключённому тобой ИИ и расходует его обычный лимит.</span>' +
        '</div>' +
      '</section>';
    overlayRoot().appendChild(modal);
    enhanceAll(modal);
    modal.querySelectorAll("[data-ai-close]").forEach(function (button) {
      button.addEventListener("click", closeModal);
    });
    modal.addEventListener("click", function (event) { if (event.target === modal) closeModal(); });
    modal.querySelector("[data-ai-run]").addEventListener("click", runAssist);
    modal.querySelector("[data-ai-accept]").addEventListener("click", acceptAssist);
    return modal;
  }

  function closeModal() {
    if (!modal) return;
    modal.hidden = true;
    document.body.style.overflow = "";
    activeField = null;
  }

  function openModal(field) {
    if (!String(field.value || "").trim()) {
      field.focus();
      field.setCustomValidity("Сначала введи текст");
      field.reportValidity();
      setTimeout(function () { field.setCustomValidity(""); }, 1200);
      return;
    }
    var dialog = createModal();
    activeField = field;
    var source = dialog.querySelector("[data-ai-source]");
    var target = dialog.querySelector("[data-ai-target]");
    source.value = detectedSource(field.value);
    var recommended = recommendedTarget(field);
    target.value = recommended;
    Array.from(target.options).forEach(function (option) {
      option.dataset.recommended = String(option.value === recommended);
    });
    dialog.querySelector("[data-ai-target-note]").textContent =
      recommended === "da" ? "датский рекомендуем" : "английский рекомендуем";
    source.dispatchEvent(new Event("change", { bubbles: true }));
    target.dispatchEvent(new Event("change", { bubbles: true }));
    dialog.querySelector("[data-ai-result]").value = "";
    dialog.querySelector("[data-ai-explanation]").hidden = true;
    dialog.querySelector("[data-ai-error]").hidden = true;
    dialog.querySelector("[data-ai-accept]").hidden = true;
    dialog.querySelector("[data-ai-run]").hidden = false;
    dialog.hidden = false;
    document.body.style.overflow = "hidden";
  }

  async function runAssist() {
    if (!activeField) return;
    var run = modal.querySelector("[data-ai-run]");
    var error = modal.querySelector("[data-ai-error]");
    var explanation = modal.querySelector("[data-ai-explanation]");
    var accept = modal.querySelector("[data-ai-accept]");
    run.disabled = true;
    run.textContent = "ИИ обрабатывает…";
    error.hidden = true;
    explanation.hidden = true;
    accept.hidden = true;
    try {
      var response = await fetch("/api/ai/text-assist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: activeField.value,
          source: modal.querySelector("[data-ai-source]").value,
          target: modal.querySelector("[data-ai-target]").value,
          mode: modal.querySelector("[data-ai-mode]").value
        })
      });
      var data = await response.json();
      if (!data.ok) throw new Error(data.error || "Не получилось обработать текст.");
      modal.querySelector("[data-ai-result]").value = data.text || "";
      explanation.textContent = data.explanation ||
        "Только перевод: смысл и факты не менялись.";
      explanation.hidden = false;
      accept.hidden = false;
      run.hidden = true;
    } catch (err) {
      error.textContent = err.message || "Не получилось обработать текст.";
      error.hidden = false;
    } finally {
      run.disabled = false;
      run.textContent = "Получить вариант";
    }
  }

  function acceptAssist() {
    if (!activeField) return;
    var result = modal.querySelector("[data-ai-result]").value;
    if (!result) return;
    var field = activeField;
    field.value = result;
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new Event("change", { bubbles: true }));
    closeModal();
    field.focus();
  }

  function enhanceWritingField(field) {
    if (!field || field.dataset.wfWriting || field.readOnly || field.disabled) return;
    if (field.matches("[data-no-ai-writing], input[type=password], input[type=email], input[type=tel], input[type=url]")) return;
    field.dataset.wfWriting = "true";
    var tools = document.createElement("div");
    tools.className = "wf-ai-field-tools";
    var button = document.createElement("button");
    button.type = "button";
    button.className = "wf-ai-write-btn";
    button.innerHTML = "<span>✦</span> Перевести / исправить";
    button.title = "Русский или украинский → английский или датский";
    button.addEventListener("click", function () { openModal(field); });
    tools.appendChild(button);
    field.insertAdjacentElement("afterend", tools);
    var language = recommendedTarget(field);
    var label = field.closest(".field") && field.closest(".field").querySelector(".lbl");
    if (label && !label.querySelector(".wf-lang-recommend")) {
      var badge = document.createElement("span");
      badge.className = "wf-lang-recommend";
      badge.textContent = language === "da" ? "датский рекомендуем" : "английский рекомендуем";
      badge.title = language === "da"
        ? "Анкета на датском: датский лучше совпадает с формой. Применяй только после проверки текста."
        : "Для англоязычной анкеты рекомендуем ответ на английском.";
      label.appendChild(badge);
    }
  }

  function enhanceWritingAll(root) {
    if (root.matches && root.matches("textarea, [data-ai-writing]")) enhanceWritingField(root);
    if (root.querySelectorAll) root.querySelectorAll("textarea, [data-ai-writing]").forEach(enhanceWritingField);
  }

  function init(root) {
    enhanceAll(root);
    enhanceWritingAll(root);
    if (root.matches && root.matches("[data-fill-value]")) bindQuickOption(root);
    if (root.querySelectorAll) root.querySelectorAll("[data-fill-value]").forEach(bindQuickOption);
  }

  function bindQuickOption(button) {
    if (button.dataset.wfFillBound) return;
    button.dataset.wfFillBound = "true";
    button.addEventListener("click", function () {
      var scope = button.closest("form") || document;
      var target = scope.querySelector('[name="' + button.dataset.fillTarget + '"]');
      if (!target) return;
      target.value = button.dataset.fillValue || "";
      target.dispatchEvent(new Event("input", { bubbles: true }));
      target.dispatchEvent(new Event("change", { bubbles: true }));
      target.focus();
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { init(document); });
  } else {
    init(document);
  }
  new MutationObserver(function (mutations) {
    mutations.forEach(function (mutation) {
      mutation.addedNodes.forEach(function (node) { if (node.nodeType === 1) init(node); });
    });
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
