/* Admin console: Core. Loaded in dependency order by admin.html. */
const TOKEN_KEY = "image_gen_admin_token";
const ADMIN_THEME_KEY = "st_imagen_admin_theme";
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const PAGE_CONFIG = {
  overview: { slug: "", sectionId: "overviewPage" },
  accounts: { slug: "accounts", sectionId: "accountsPage" },
  users: { slug: "users", sectionId: "usersPage" },
  invites: { slug: "invites", sectionId: "invitesPage" },
  logs: { slug: "logs", sectionId: "logsPage" },
  settings: { slug: "settings", sectionId: "settingsPage" },
};
const PAGE_KEYS = Object.keys(PAGE_CONFIG);
const DEFAULT_FILTERS = {
  accounts: { query: "", status: "all", load: "all" },
  users: { query: "", status: "all", lifecycle: "all" },
  invites: { query: "", status: "all" },
  logs: { query: "", status: "all", mode: "all", category: "all" },
};
const FAILURE_CATEGORY_LABELS = {
  capacity: "容量 / 限流",
  account_config: "账号 / 配置",
  reference_input: "参考图 / 输入",
  upstream: "上游服务",
  storage: "存储 / 落盘",
  other: "其他",
};
const adminBasePath = (() => {
  const segments = window.location.pathname.split("/").filter(Boolean);
  return segments.length ? `/${segments[0]}` : "/admin";
})();

function cloneDefaultFilters() {
  return JSON.parse(JSON.stringify(DEFAULT_FILTERS));
}

const state = {
  admin: null,
  overview: null,
  runtimeStatus: null,
  runtimeMetrics: null,
  runtimeMetricsUpdatedAt: null,
  runtimeConfig: null,
  accounts: [],
  users: [],
  invites: [],
  logs: [],
  refreshing: false,
  lastUpdatedAt: null,
  sync: {
    status: "idle",
    successCount: 0,
    totalCount: 0,
  },
  editing: {
    accountId: null,
    userId: null,
  },
  filters: cloneDefaultFilters(),
  page: "overview",
  previewVisible: false,
  previewItems: [],
  previewIndex: -1,
  previewPrompt: "",
  previewError: "",
  previewReturnFocus: null,
  modalReturnFocus: null,
  userCreateMode: "manual",
  dashboardMetrics: null,
  dashboardAnalytics: null,
  dashboardPeriod: "24h",
  dashboardAnalyticsLoading: false,
  dashboardAnalyticsRequestId: 0,
  dashboardAnalyticsAbortController: null,
  dashboardSnapshotLoading: false,
  batchRendering: false,
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function shortAccount(name) {
  if (!name) return "—";
  return name.includes("@") ? name.split("@")[0] : name;
}

function fmtNumber(value) {
  const num = Number(value || 0);
  return Number.isFinite(num) ? num.toLocaleString("zh-CN") : "0";
}

function parseApiDate(value) {
  if (window.STImagen?.parseApiDate) {
    return window.STImagen.parseApiDate(value);
  }
  try {
    if (!value) return null;
    if (value instanceof Date) {
      return Number.isNaN(value.getTime()) ? null : new Date(value.getTime());
    }
    const text = String(value).trim();
    if (!text) return null;
    const normalized =
      /(?:[zZ]|[+-]\d{2}:\d{2})$/.test(text) || !/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(text)
        ? text
        : `${text.replace(" ", "T")}Z`;
    const date = new Date(normalized);
    return Number.isNaN(date.getTime()) ? null : date;
  } catch {
    return null;
  }
}

function getShanghaiDateParts(value) {
  if (window.STImagen?.getShanghaiDateParts) {
    return window.STImagen.getShanghaiDateParts(value);
  }
  const date = parseApiDate(value);
  if (!date) return null;
  const parts = {};
  new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  })
    .formatToParts(date)
    .forEach((part) => {
      if (part.type !== "literal") parts[part.type] = part.value;
    });
  return parts;
}

function formatInShanghai(value, options) {
  if (window.STImagen?.formatInShanghai) {
    return window.STImagen.formatInShanghai(value, options);
  }
  try {
    const date = parseApiDate(value);
    if (!date) return "";
    return new Intl.DateTimeFormat(
      "zh-CN",
      Object.assign(
        {
          timeZone: "Asia/Shanghai",
          hour12: false,
        },
        options || {},
      ),
    ).format(date);
  } catch {
    return "";
  }
}

function fmtDate(value) {
  if (!value) return "—";
  try {
    const formatted = formatInShanghai(value, {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    if (!formatted) return String(value);
    return formatted;
  } catch {
    return String(value);
  }
}

function fmtDateLong(value) {
  if (!value) return "—";
  try {
    const formatted = formatInShanghai(value, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    if (!formatted) return String(value);
    return formatted;
  } catch {
    return String(value);
  }
}

function fmtTime(value) {
  if (!value) return "—";
  try {
    const formatted = formatInShanghai(value, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    if (!formatted) return String(value);
    return formatted;
  } catch {
    return String(value);
  }
}

function fmtRelativeTime(value) {
  if (!value) return "—";
  try {
    const date = parseApiDate(value);
    if (!date) return String(value);
    const diffMs = date.getTime() - Date.now();
    const absMs = Math.abs(diffMs);
    const units = [
      { label: "天", value: 24 * 60 * 60 * 1000 },
      { label: "小时", value: 60 * 60 * 1000 },
      { label: "分钟", value: 60 * 1000 },
    ];
    for (const unit of units) {
      if (absMs >= unit.value) {
        const amount = Math.round(absMs / unit.value);
        return diffMs >= 0 ? `${amount}${unit.label}后` : `${amount}${unit.label}前`;
      }
    }
    return diffMs >= 0 ? "即将" : "刚刚";
  } catch {
    return String(value);
  }
}

function fmtDuration(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value)) return "—";
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 10_000 ? 1 : 2)}s`;
  return `${Math.round(value)}ms`;
}

function fmtQuota(used, total) {
  const safeUsed = Number(used || 0);
  const safeTotal = Number(total || 0);
  if (!safeTotal) return `${fmtNumber(safeUsed)} / ∞`;
  return `${fmtNumber(safeUsed)} / ${fmtNumber(safeTotal)}`;
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function percentage(value, total) {
  if (!total || total <= 0) return 0;
  return clamp((Number(value || 0) / Number(total || 1)) * 100, 0, 100);
}

function avg(values) {
  const items = values.filter((value) => Number.isFinite(Number(value))).map((value) => Number(value));
  if (!items.length) return 0;
  return items.reduce((sum, value) => sum + value, 0) / items.length;
}

function sumBy(items, getter) {
  return items.reduce((sum, item) => sum + Number(getter(item) || 0), 0);
}

function isoToDatetimeLocal(value) {
  if (!value) return "";
  const parts = getShanghaiDateParts(value);
  if (!parts) return "";
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

function datetimeLocalToIso(value) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) throw new Error("到期时间格式不正确");
  return date.toISOString();
}

function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

function setToken(token) {
  if (!token) localStorage.removeItem(TOKEN_KEY);
  else localStorage.setItem(TOKEN_KEY, token);
}

function applyAdminTheme(theme) {
  const next = theme === "dark" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", next);
  try {
    localStorage.setItem(ADMIN_THEME_KEY, next);
  } catch (_) {}
}

function bindAdminThemeToggle() {
  const button = $("#themeToggle");
  if (!button) return;
  button.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
    applyAdminTheme(current === "light" ? "dark" : "light");
  });
}

function showToast(message, type = "info") {
  const stack = $("#toastStack");
  if (!stack) return;
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  stack.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add("show"));
  window.setTimeout(() => {
    toast.classList.remove("show");
    window.setTimeout(() => toast.remove(), 220);
  }, 2800);
}

async function copyTextToClipboard(text) {
  const value = String(text || "");
  if (!value) throw new Error("empty");
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }

  const ta = document.createElement("textarea");
  ta.value = value;
  ta.setAttribute("readonly", "readonly");
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  ta.style.pointerEvents = "none";
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, ta.value.length);
  const ok = document.execCommand("copy");
  document.body.removeChild(ta);
  if (!ok) {
    throw new Error("copy_failed");
  }
}

function badgeHtml(text, variant = "", extraClass = "") {
  const classes = ["badge"];
  if (variant) classes.push(variant);
  if (extraClass) classes.push(extraClass);
  return `<span class="${classes.join(" ")}">${escapeHtml(text)}</span>`;
}

function inviteStatusBadge(status) {
  if (status === "active") return badgeHtml("active", "success");
  if (status === "revoked") return badgeHtml("revoked", "danger");
  if (status === "expired") return badgeHtml("expired", "warning");
  if (status === "exhausted") return badgeHtml("exhausted", "warning");
  return badgeHtml(status || "unknown");
}

function userStatusBadge(status) {
  if (status === "active") return badgeHtml("active", "success");
  if (status === "expired") return badgeHtml("expired", "warning");
  if (status === "disabled") return badgeHtml("disabled", "danger");
  return badgeHtml(status || "unknown");
}

function accountStatusBadge(status) {
  return status === "active" ? badgeHtml("active", "success") : badgeHtml("disabled", "danger");
}

function toggleActionMeta(status) {
  return status === "active"
    ? { nextStatus: "disabled", label: "停用", className: "btn btn-ghost" }
    : { nextStatus: "active", label: "启用", className: "btn btn-primary" };
}

function isMissingDeleteRouteError(err) {
  return (
    !!err &&
    (err.status === 404 || err.status === 405) &&
    (err.message === "Not Found" || err.message === "Method Not Allowed")
  );
}

function isUrlLike(value) {
  const text = String(value || "").trim();
  return /^https?:\/\//i.test(text) || text.startsWith("/") || text.startsWith("data:image/");
}

function parseOutputImages(rawValue, fallback) {
  const values = [];
  const pushUrl = (value) => {
    const text = String(value || "").trim();
    if (text && isUrlLike(text) && !values.includes(text)) values.push(text);
  };

  if (Array.isArray(rawValue)) {
    rawValue.forEach(pushUrl);
  } else if (typeof rawValue === "string" && rawValue.trim()) {
    const text = rawValue.trim();
    if (text.startsWith("[") || text.startsWith("{")) {
      try {
        const parsed = JSON.parse(text);
        if (Array.isArray(parsed)) parsed.forEach(pushUrl);
        else if (parsed && typeof parsed === "object") {
          if (Array.isArray(parsed.images)) parsed.images.forEach(pushUrl);
          if (parsed.url) pushUrl(parsed.url);
        } else {
          pushUrl(parsed);
        }
      } catch {
        pushUrl(text);
      }
    } else {
      pushUrl(text);
    }
  } else if (rawValue) {
    pushUrl(rawValue);
  }

  pushUrl(fallback);
  return values;
}

function humanMode(mode) {
  return mode === "img2img" ? "图生图" : "文生图";
}

function truncateText(value, maxLength = 72) {
  const text = String(value || "").trim();
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength - 1)}…`;
}

function computeAccountLoadMeta(account) {
  const current = Number(account?.in_flight || 0);
  const max = Math.max(1, Number(account?.max_inflight || 1));
  const ratio = current / max;
  if (account?.status !== "active") {
    return { label: "停用", variant: "danger", ratio: 0 };
  }
  if (ratio >= 1) return { label: "已打满", variant: "danger", ratio };
  if (ratio >= 0.6) return { label: "繁忙", variant: "warning", ratio };
  if (ratio > 0) return { label: "处理中", variant: "success", ratio };
  return { label: "空闲", variant: "", ratio };
}

function computeUserLifecycle(user) {
  if (user?.effective_status === "expired" || user?.is_expired) return "expired";
  if (user?.status === "disabled") return "disabled";
  return "active";
}

function isExpiringSoon(user, days = 7) {
  if (!user?.expires_at) return false;
  const expiresAt = parseApiDate(user.expires_at);
  if (!expiresAt) return false;
  const diff = expiresAt.getTime() - Date.now();
  return diff > 0 && diff <= days * 24 * 60 * 60 * 1000;
}

function renderCapacityBar(current, total) {
  const safeTotal = Math.max(1, Number(total || 1));
  const safeCurrent = Number(current || 0);
  return `
    <div class="capacity-bar">
      <span class="capacity-fill" style="width:${percentage(safeCurrent, safeTotal).toFixed(2)}%"></span>
    </div>
  `;
}

function renderEmptyRow(colspan, title, detail) {
  return `
    <tr>
      <td colspan="${colspan}" class="empty-row-cell">
        <div class="empty-row">
          <strong>${escapeHtml(title)}</strong>
          <span>${escapeHtml(detail || "")}</span>
        </div>
      </td>
    </tr>
  `;
}

function renderErrorRow(colspan, message) {
  return `
    <tr>
      <td colspan="${colspan}" class="empty-row-cell">
        <div class="empty-row error-row">
          <strong>加载失败</strong>
          <span>${escapeHtml(message)}</span>
        </div>
      </td>
    </tr>
  `;
}

function filterChipHtml(label, value, tone = "") {
  const classes = ["filter-chip"];
  if (tone) classes.push(`filter-chip-${tone}`);
  return `
    <span class="${classes.join(" ")}">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </span>
  `;
}

function renderToolbarMeta(id, noun, shownCount, totalCount, chips, resetKey, hint) {
  const el = $(`#${id}`);
  if (!el) return;
  const hasFilters = chips.length > 0;
  const summary =
    totalCount > 0
      ? hasFilters
        ? `显示 ${fmtNumber(shownCount)} / ${fmtNumber(totalCount)} 个${noun}`
        : `当前展示全部 ${fmtNumber(totalCount)} 个${noun}`
      : `当前还没有${noun}数据`;
  const detail = hasFilters ? "已应用筛选条件" : hint;

  el.innerHTML = `
    <div class="toolbar-meta-main">
      <strong>${summary}</strong>
      <span>${escapeHtml(detail)}</span>
    </div>
    <div class="filter-chip-row">
      ${chips.join("") || '<span class="filter-chip filter-chip-muted">当前无筛选</span>'}
      ${hasFilters ? `<button class="filter-reset" data-filter-reset="${escapeHtml(resetKey)}" type="button">清空筛选</button>` : ""}
    </div>
  `;

  const resetButton = el.querySelector("[data-filter-reset]");
  if (resetButton) {
    resetButton.addEventListener("click", () => resetFilterGroup(resetKey));
  }
}

function syncFilterInputs(group) {
  if (group === "accounts") {
    $("#accountSearchInput").value = state.filters.accounts.query;
    $("#accountStatusFilter").value = state.filters.accounts.status;
    $("#accountLoadFilter").value = state.filters.accounts.load;
    return;
  }
  if (group === "users") {
    $("#userSearchInput").value = state.filters.users.query;
    $("#userStatusFilter").value = state.filters.users.status;
    $("#userLifecycleFilter").value = state.filters.users.lifecycle;
    return;
  }
  if (group === "invites") {
    $("#inviteSearchInput").value = state.filters.invites.query;
    $("#inviteStatusFilter").value = state.filters.invites.status;
    return;
  }
  if (group === "logs") {
    $("#logSearchInput").value = state.filters.logs.query;
    $("#logStatusFilter").value = state.filters.logs.status;
    $("#logModeFilter").value = state.filters.logs.mode;
    $("#logFailureCategoryFilter").value = state.filters.logs.category;
  }
}

function rerenderByFilterGroup(group) {
  if (group === "accounts") {
    renderAccountsTable();
    return;
  }
  if (group === "users") {
    renderUsersTable();
    return;
  }
  if (group === "invites") {
    renderInvitesTable();
    return;
  }
  if (group === "logs") renderLogsTable();
}

function resetFilterGroup(group) {
  if (!DEFAULT_FILTERS[group]) return;
  state.filters[group] = Object.assign({}, DEFAULT_FILTERS[group]);
  syncFilterInputs(group);
  rerenderByFilterGroup(group);
}

function normalizePageKey(pageKey) {
  return PAGE_CONFIG[pageKey] ? pageKey : "overview";
}

function currentPageFromLocation(pathname = window.location.pathname) {
  const segments = pathname.split("/").filter(Boolean);
  if (segments.length <= 1) return "overview";
  const slug = segments.slice(1).join("/");
  const match = PAGE_KEYS.find((key) => PAGE_CONFIG[key].slug === slug);
  return normalizePageKey(match);
}

function buildAdminPageHref(pageKey) {
  const key = normalizePageKey(pageKey);
  const slug = PAGE_CONFIG[key].slug;
  return slug ? `${adminBasePath}/${slug}` : adminBasePath;
}

function setActiveConsoleNav(pageKey) {
  $$("[data-page-link]").forEach((link) => {
    const active = link.dataset.pageLink === pageKey;
    link.classList.toggle("is-active", active);
    if (active) link.setAttribute("aria-current", "true");
    else link.removeAttribute("aria-current");
  });
}

function setNavBadge(id, text, tone = "") {
  const el = $(`#${id}`);
  if (!el) return;
  el.textContent = text;
  el.className = "console-nav-badge";
  if (tone) el.classList.add(`is-${tone}`);
}

function showAdminPage(pageKey, options = {}) {
  const key = normalizePageKey(pageKey);
  state.page = key;
  $$(".admin-page").forEach((page) => {
    page.classList.toggle("is-hidden", page.id !== PAGE_CONFIG[key].sectionId);
  });
  setActiveConsoleNav(key);
  closeAdminNav();

  if (options.updateHistory) {
    const href = buildAdminPageHref(key);
    const mode = options.replace ? "replaceState" : "pushState";
    if (window.location.pathname !== href) {
      window.history[mode]({ page: key }, "", href);
    }
  }

  if (options.scroll !== false) window.scrollTo({ top: 0, behavior: "auto" });
}

function initConsoleNav() {
  const links = $$("[data-page-link]");
  if (!links.length) return;

  links.forEach((link) => {
    const pageKey = normalizePageKey(link.dataset.pageLink);
    link.href = buildAdminPageHref(pageKey);
    link.addEventListener("click", (event) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
      event.preventDefault();
      showAdminPage(pageKey, { updateHistory: true });
    });
  });

  window.addEventListener("popstate", () => {
    showAdminPage(currentPageFromLocation(), { scroll: false });
  });

  const initialPage = currentPageFromLocation();
  const expectedPath = buildAdminPageHref(initialPage);
  const currentPath = window.location.pathname.replace(/\/+$/, "") || "/";
  showAdminPage(initialPage, { scroll: false });
  if (currentPath !== expectedPath) {
    showAdminPage(initialPage, { replace: true, updateHistory: true, scroll: false });
  }
}

function closeAdminNav() {
  document.body.classList.remove("admin-nav-open");
  const toggle = $("#adminMenuToggle");
  if (toggle) toggle.setAttribute("aria-expanded", "false");
}

function openAdminNav() {
  document.body.classList.add("admin-nav-open");
  const toggle = $("#adminMenuToggle");
  if (toggle) toggle.setAttribute("aria-expanded", "true");
  $("#adminNavClose")?.focus({ preventScroll: true });
}

function searchAdminData(query) {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return [];
  const results = [];
  state.accounts.forEach((item) => {
    if ([item.name, item.org_id, item.flow_id].some((value) => String(value || "").toLowerCase().includes(normalized))) {
      results.push({ page: "accounts", title: item.name || "账号", detail: "账号池", query: item.name || normalized });
    }
  });
  state.users.forEach((item) => {
    if (String(item.username || "").toLowerCase().includes(normalized)) results.push({ page: "users", title: item.username, detail: "用户", query: item.username });
  });
  state.invites.forEach((item) => {
    if ([item.code_prefix, item.note].some((value) => String(value || "").toLowerCase().includes(normalized))) results.push({ page: "invites", title: item.code_prefix || "邀请码", detail: "邀请码", query: item.code_prefix || normalized });
  });
  state.logs.forEach((item) => {
    if ([item.prompt_preview, item.username, item.account_name, item.model, item.error_message].some((value) => String(value || "").toLowerCase().includes(normalized))) results.push({ page: "logs", title: item.model || humanMode(item.mode), detail: `${item.username || "匿名用户"} · ${fmtRelativeTime(item.timestamp)}`, query: normalized });
  });
  return results.slice(0, 8);
}

function renderGlobalSearch() {
  const input = $("#globalSearchInput");
  const resultsEl = $("#adminSearchResults");
  if (!input || !resultsEl) return;
  const query = input.value.trim();
  if (!query) {
    resultsEl.classList.add("is-hidden");
    resultsEl.innerHTML = "";
    return;
  }
  const results = searchAdminData(query);
  resultsEl.classList.remove("is-hidden");
  resultsEl.innerHTML = results.length
    ? results.map((item, index) => `<button class="admin-search-result" type="button" data-search-index="${index}"><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.detail)}</small></button>`).join("")
    : '<div class="admin-search-result"><strong>没有匹配数据</strong><small>只搜索当前已加载的账号、用户、邀请码和日志</small></div>';
  resultsEl.querySelectorAll("[data-search-index]").forEach((button) => {
    button.addEventListener("click", () => {
      const item = results[Number(button.dataset.searchIndex)];
      if (!item) return;
      state.filters[item.page].query = item.query;
      syncFilterInputs(item.page);
      rerenderByFilterGroup(item.page);
      showAdminPage(item.page, { updateHistory: true });
      resultsEl.classList.add("is-hidden");
    });
  });
}

function bindAdminNavigation() {
  $("#adminMenuToggle")?.addEventListener("click", () => {
    if (document.body.classList.contains("admin-nav-open")) closeAdminNav();
    else openAdminNav();
  });
  $("#adminNavClose")?.addEventListener("click", closeAdminNav);
  $("#adminNavScrim")?.addEventListener("click", closeAdminNav);
  const input = $("#globalSearchInput");
  if (input) input.value = "";
  input?.addEventListener("input", renderGlobalSearch);
  input?.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      input.value = "";
      renderGlobalSearch();
      input.blur();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !(event.target instanceof HTMLInputElement) && !(event.target instanceof HTMLTextAreaElement)) {
      event.preventDefault();
      input?.focus();
    }
    if (event.key === "Escape" && document.body.classList.contains("admin-nav-open")) closeAdminNav();
  });
  document.addEventListener("click", (event) => {
    const protectionLink = event.target instanceof Element ? event.target.closest("[data-protection-page]") : null;
    if (protectionLink && !(event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0)) {
      event.preventDefault();
      showAdminPage(protectionLink.dataset.protectionPage, { updateHistory: true });
    }
    if (!event.target.closest(".admin-topbar-search")) $("#adminSearchResults")?.classList.add("is-hidden");
    const tableMore = event.target instanceof Element ? event.target.closest(".table-more") : null;
    closeTableMoreMenus(tableMore);
  });
}

function setModalVisible(id, visible) {
  const el = typeof id === "string" ? $(`#${id}`) : id;
  if (!el) return;
  const wasVisible = el.classList.contains("show");
  if (visible && !wasVisible && document.activeElement instanceof HTMLElement) {
    state.modalReturnFocus = document.activeElement;
  }
  el.classList.toggle("show", visible);
  el.setAttribute("aria-hidden", visible ? "false" : "true");
  document.body.classList.toggle("modal-open", $$(".modal-mask.show").length > 0);
  if (!visible && wasVisible) {
    const returnFocusEl = state.modalReturnFocus;
    state.modalReturnFocus = null;
    if (returnFocusEl && document.body.contains(returnFocusEl)) returnFocusEl.focus({ preventScroll: true });
  }
}

function closeAllModals() {
  closeLogModal({ restoreFocus: false });
  $$(".modal-mask.show").forEach((el) => el.classList.remove("show"));
  $$(".modal-mask[aria-hidden='false']").forEach((el) => el.setAttribute("aria-hidden", "true"));
  document.body.classList.remove("modal-open");
}

async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, Object.assign({}, opts, { headers }));
  if (response.status === 401) {
    setToken("");
    showLogin();
    throw new Error("未登录或登录已过期");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message =
      typeof data.detail === "string"
        ? data.detail
        : data.detail?.message || data.message || `HTTP ${response.status}`;
    const err = new Error(message);
    err.status = response.status;
    err.payload = data;
    throw err;
  }
  return data;
}

async function withBusyButton(button, pendingText, task) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = pendingText;
  try {
    return await task();
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function showLogin() {
  closeAllModals();
  document.body.classList.add("admin-login-state");
  const searchInput = $("#globalSearchInput");
  if (searchInput) searchInput.value = "";
  $("#adminSearchResults")?.classList.add("is-hidden");
  $("#loginSection").classList.remove("is-hidden");
  $("#dashboardSection").classList.add("is-hidden");
  $("#logoutLink").classList.add("is-hidden");
}

function showDashboard() {
  closeAllModals();
  document.body.classList.remove("admin-login-state");
  $("#loginSection").classList.add("is-hidden");
  $("#dashboardSection").classList.remove("is-hidden");
  $("#logoutLink").classList.remove("is-hidden");
  showAdminPage(currentPageFromLocation(), { scroll: false });
  refreshAll();
  startRuntimeMetricsPolling();
  startRuntimeStatusPolling();
  startDashboardRefreshPolling();
}

async function login() {
  const username = $("#loginUsername").value.trim();
  const password = $("#loginPassword").value;
  const errEl = $("#loginError");
  errEl.classList.add("is-hidden");

  if (!username || !password) {
    errEl.textContent = "请输入用户名和密码";
    errEl.classList.remove("is-hidden");
    return;
  }

  try {
    const data = await api("/api/admin/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    if (!data.success) throw new Error(data.message || "登录失败");
    stopLoginLockoutCountdown();
    setToken(data.token);
    showToast("登录成功", "success");
    showDashboard();
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove("is-hidden");
    const retryAfter = Number(err.payload?.detail?.retry_after) || 0;
    if (err.status === 429 && retryAfter > 0) startLoginLockoutCountdown(retryAfter);
  }
}

let _loginLockoutTimer = null;

function startLoginLockoutCountdown(seconds) {
  stopLoginLockoutCountdown();
  const button = $("#loginBtn");
  if (!button) return;
  let remain = Math.min(Math.ceil(seconds), 24 * 3600);
  const tick = () => {
    if (remain <= 0) {
      stopLoginLockoutCountdown();
      return;
    }
    button.disabled = true;
    button.textContent = `请等待 ${remain}s`;
    remain -= 1;
  };
  tick();
  _loginLockoutTimer = window.setInterval(tick, 1000);
}

function stopLoginLockoutCountdown() {
  if (_loginLockoutTimer) {
    window.clearInterval(_loginLockoutTimer);
    _loginLockoutTimer = null;
  }
  const button = $("#loginBtn");
  if (button) {
    button.disabled = false;
    button.textContent = "登录";
  }
}

function logout() {
  setToken("");
  stopRuntimeMetricsPolling();
  stopRuntimeStatusPolling();
  stopDashboardRefreshPolling();
  showToast("已退出后台控制台", "info");
  showLogin();
}

