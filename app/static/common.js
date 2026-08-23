/**
 * 共享前端工具：主题切换 + 自定义下拉增强。
 *
 * 通过 window.STImagen 暴露给 app.js / admin.js。
 */
(function () {
  "use strict";

  // ==================== Theme ====================
  var STORAGE_KEY = "theme";

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch (_) {}
  }

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "light"
      ? "light"
      : "dark";
  }

  function bindThemeToggle(buttonId) {
    var btn = document.getElementById(buttonId || "themeToggle");
    if (!btn) return;
    btn.addEventListener("click", function () {
      applyTheme(currentTheme() === "light" ? "dark" : "light");
    });
  }

  // ==================== Timezone helpers ====================
  var SHANGHAI_TIME_ZONE = "Asia/Shanghai";

  function normalizeApiDateInput(value) {
    if (typeof value !== "string") return value;
    var text = value.trim();
    if (!text) return text;
    var hasExplicitTimezone = /(?:[zZ]|[+-]\d{2}:\d{2})$/.test(text);
    if (!hasExplicitTimezone && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(text)) {
      return text.replace(" ", "T") + "Z";
    }
    return text;
  }

  function parseApiDate(value) {
    if (!value) return null;
    var normalized = value instanceof Date ? new Date(value.getTime()) : normalizeApiDateInput(value);
    var date = normalized instanceof Date ? normalized : new Date(normalized);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatInShanghai(value, options) {
    var date = parseApiDate(value);
    if (!date) return "";
    return new Intl.DateTimeFormat(
      "zh-CN",
      Object.assign(
        {
          timeZone: SHANGHAI_TIME_ZONE,
          hour12: false,
        },
        options || {}
      )
    ).format(date);
  }

  function getShanghaiDateParts(value) {
    var date = parseApiDate(value);
    if (!date) return null;
    var parts = {};
    new Intl.DateTimeFormat("zh-CN", {
      timeZone: SHANGHAI_TIME_ZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    })
      .formatToParts(date)
      .forEach(function (part) {
        if (part.type !== "literal") parts[part.type] = part.value;
      });
    return parts;
  }

  // ==================== Custom select ====================
  // 把一个原生 <select> 增强为视觉自定义下拉。
  //  - 原生 select 视觉隐藏（绝对定位 opacity:0），仍参与表单与 a11y；
  //  - 同级注入 .select-display + .select-chevron + .select-panel；
  //  - 通过 MutationObserver 同步外部对 options 的修改（兼容现有 fillSelect）。
  function enhanceSelect(selectEl) {
    if (!selectEl || selectEl.dataset.enhanced === "1") return;
    var control = selectEl.closest(".control");
    if (!control) return;

    selectEl.dataset.enhanced = "1";
    control.classList.add("has-select");
    selectEl.classList.add("select-native");
    selectEl.setAttribute("tabindex", "-1");

    var display = document.createElement("span");
    display.className = "select-display";

    var SVG_NS = "http://www.w3.org/2000/svg";
    var chevron = document.createElementNS(SVG_NS, "svg");
    chevron.setAttribute("class", "select-chevron");
    chevron.setAttribute("viewBox", "0 0 24 24");
    chevron.setAttribute("fill", "none");
    chevron.setAttribute("stroke", "currentColor");
    chevron.setAttribute("stroke-width", "2");
    chevron.setAttribute("stroke-linecap", "round");
    chevron.setAttribute("stroke-linejoin", "round");
    var chevronPath = document.createElementNS(SVG_NS, "path");
    chevronPath.setAttribute("d", "m6 9 6 6 6-6");
    chevron.appendChild(chevronPath);

    var panel = document.createElement("div");
    panel.className = "select-panel";
    panel.setAttribute("role", "listbox");

    // 让 display 与 chevron 替代隐藏的 select 占位（成为 flex item）
    selectEl.parentNode.insertBefore(display, selectEl);
    selectEl.parentNode.insertBefore(chevron, selectEl.nextSibling);
    selectEl.parentNode.appendChild(panel);

    var isOpen = false;

    function syncDisplay() {
      var opt = selectEl.options[selectEl.selectedIndex];
      display.textContent = opt ? opt.label : "—";
    }

    function syncPanel() {
      panel.innerHTML = "";
      Array.prototype.forEach.call(selectEl.options, function (opt) {
        var item = document.createElement("button");
        item.type = "button";
        item.className = "select-option" + (opt.selected ? " is-selected" : "");
        item.textContent = opt.label;
        item.dataset.value = opt.value;
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", opt.selected ? "true" : "false");
        item.addEventListener("click", function (e) {
          e.stopPropagation();
          if (selectEl.value !== opt.value) {
            selectEl.value = opt.value;
            selectEl.dispatchEvent(new Event("change", { bubbles: true }));
          }
          close();
        });
        panel.appendChild(item);
      });
    }

    // 视口感知定位：offsetWidth/offsetHeight 反映布局尺寸，不受打开过渡的 transform 影响
    function positionPanel() {
      // 先复位为默认向下、向右展开，保证每次打开都基于当前视口重新计算
      control.classList.remove("flip-up");
      control.classList.remove("flip-right");
      panel.style.maxHeight = "";

      var PANEL_GAP = 8; // 面板与控件的间距，与 CSS calc(100% + 8px) 保持一致
      var VIEWPORT_EDGE = 12; // 距视口边缘的安全距离
      var MIN_PANEL_HEIGHT = 120; // 视口极矮时保留的最小可用高度

      var controlRect = control.getBoundingClientRect();
      var spaceBelow = window.innerHeight - controlRect.bottom - PANEL_GAP - VIEWPORT_EDGE;
      var spaceAbove = controlRect.top - PANEL_GAP - VIEWPORT_EDGE;

      if (panel.offsetHeight > spaceBelow && spaceAbove > spaceBelow) {
        control.classList.add("flip-up");
      }
      var available = control.classList.contains("flip-up") ? spaceAbove : spaceBelow;
      if (available < panel.offsetHeight) {
        panel.style.maxHeight = Math.max(available, MIN_PANEL_HEIGHT) + "px";
      }
      if (controlRect.left + panel.offsetWidth > window.innerWidth - VIEWPORT_EDGE) {
        control.classList.add("flip-right");
      }
    }

    function open() {
      if (isOpen) return;
      syncPanel();
      positionPanel();
      control.classList.add("is-open");
      isOpen = true;
      requestAnimationFrame(function () {
        document.addEventListener("click", onDocClick, true);
        document.addEventListener("keydown", onDocKey);
        window.addEventListener("resize", onReposition);
        window.addEventListener("scroll", onReposition, true);
      });
    }

    function close() {
      if (!isOpen) return;
      control.classList.remove("is-open");
      isOpen = false;
      document.removeEventListener("click", onDocClick, true);
      document.removeEventListener("keydown", onDocKey);
      window.removeEventListener("resize", onReposition);
      window.removeEventListener("scroll", onReposition, true);
    }

    // 打开期间视口变化（滚动、缩放）时重新定位
    function onReposition() {
      if (isOpen) positionPanel();
    }

    function onDocClick(e) {
      if (!control.contains(e.target)) close();
    }

    function onDocKey(e) {
      if (e.key === "Escape") {
        close();
        control.focus();
      }
    }

    control.addEventListener("click", function (e) {
      if (panel.contains(e.target)) return;
      if (isOpen) close();
      else open();
    });

    if (!control.hasAttribute("tabindex")) control.setAttribute("tabindex", "0");
    control.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (isOpen) close();
        else open();
      } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        var len = selectEl.options.length;
        if (!len) return;
        var dir = e.key === "ArrowDown" ? 1 : -1;
        selectEl.selectedIndex =
          (selectEl.selectedIndex + dir + len) % len;
        selectEl.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });

    var observer = new MutationObserver(function () {
      syncDisplay();
      if (isOpen) {
        syncPanel();
        positionPanel();
      }
    });
    observer.observe(selectEl, { childList: true, subtree: false });

    selectEl.addEventListener("change", function () {
      syncDisplay();
      if (isOpen) syncPanel();
    });

    syncDisplay();
  }

  function enhanceAllSelects(rootSelector) {
    var root = rootSelector ? document.querySelector(rootSelector) : document;
    if (!root) return;
    root.querySelectorAll(".control select").forEach(function (sel) {
      enhanceSelect(sel);
    });
  }

  // ==================== Expose ====================
  window.STImagen = window.STImagen || {};
  window.STImagen.bindThemeToggle = bindThemeToggle;
  window.STImagen.parseApiDate = parseApiDate;
  window.STImagen.formatInShanghai = formatInShanghai;
  window.STImagen.getShanghaiDateParts = getShanghaiDateParts;
  window.STImagen.enhanceSelect = enhanceSelect;
  window.STImagen.enhanceAllSelects = enhanceAllSelects;
})();
