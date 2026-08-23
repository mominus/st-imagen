const TOKEN_KEY = "image_gen_admin_token";
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
  logs: { query: "", status: "all", mode: "all" },
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
  userCreateMode: "manual",
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

function setModalVisible(id, visible) {
  const el = typeof id === "string" ? $(`#${id}`) : id;
  if (!el) return;
  el.classList.toggle("show", visible);
  document.body.classList.toggle("modal-open", $$(".modal-mask.show").length > 0);
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
  $("#loginSection").classList.remove("is-hidden");
  $("#dashboardSection").classList.add("is-hidden");
  $("#logoutLink").classList.add("is-hidden");
}

function showDashboard() {
  closeAllModals();
  $("#loginSection").classList.add("is-hidden");
  $("#dashboardSection").classList.remove("is-hidden");
  $("#logoutLink").classList.remove("is-hidden");
  showAdminPage(currentPageFromLocation(), { scroll: false });
  refreshAll();
  startRuntimeStatusPolling();
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
  stopRuntimeStatusPolling();
  showToast("已退出后台控制台", "info");
  showLogin();
}

function deriveMetrics() {
  const accounts = state.accounts;
  const users = state.users;
  const invites = state.invites;
  const logs = state.logs;
  const overview = state.overview || {};

  const totalAccounts = overview.accounts?.total ?? accounts.length;
  const activeAccounts = overview.accounts?.active ?? accounts.filter((item) => item.status === "active").length;
  const disabledAccounts = Math.max(0, totalAccounts - activeAccounts);
  const accountSlotsUsed = sumBy(accounts, (item) => item.in_flight);
  const accountSlotsTotal = sumBy(accounts, (item) => item.max_inflight);
  const saturatedAccounts = accounts.filter((item) => computeAccountLoadMeta(item).label === "已打满").length;

  const totalUsers = overview.users?.total ?? users.length;
  const activeUsers = overview.users?.active ?? users.filter((item) => computeUserLifecycle(item) === "active").length;
  const expiredUsers = users.filter((item) => computeUserLifecycle(item) === "expired").length;
  const disabledUsers = users.filter((item) => computeUserLifecycle(item) === "disabled").length;
  const expiringUsers = users.filter((item) => isExpiringSoon(item)).length;
  const userInflight = sumBy(users, (item) => item.in_flight);
  const userCapacity = sumBy(users, (item) => item.max_inflight);

  const totalInvites = overview.invites?.total ?? invites.length;
  const activeInvites = overview.invites?.active ?? invites.filter((item) => item.status === "active").length;
  const inviteRemainingUses = invites.reduce(
    (sum, invite) => sum + Math.max(0, Number(invite.max_uses || 0) - Number(invite.used_count || 0)),
    0,
  );
  const exhaustedInvites = invites.filter((item) => item.status === "exhausted").length;

  const totalGenerations = overview.generations?.total ?? logs.length;
  const successGenerations =
    overview.generations?.success ?? logs.filter((item) => item.status === "success").length;
  const errorGenerations =
    overview.generations?.error ?? logs.filter((item) => item.status === "error").length;
  const successRate = totalGenerations ? (successGenerations / totalGenerations) * 100 : 0;
  const streamCount = logs.filter((item) => item.is_stream).length;
  const avgDuration = avg(logs.map((item) => item.response_time_ms));
  const logs24h = logs.filter((item) => {
    if (!item.timestamp) return false;
    const ts = parseApiDate(item.timestamp)?.getTime();
    return Number.isFinite(ts) && Date.now() - ts <= 24 * 60 * 60 * 1000;
  });
  const lastSuccess = logs.find((item) => item.status === "success");
  const lastError = logs.find((item) => item.status === "error");
  const busiestAccounts = [...accounts]
    .sort((a, b) => {
      const ratioA = percentage(a.in_flight, a.max_inflight);
      const ratioB = percentage(b.in_flight, b.max_inflight);
      if (ratioB !== ratioA) return ratioB - ratioA;
      return Number(b.total_requests || 0) - Number(a.total_requests || 0);
    })
    .slice(0, 4);
  const recentErrors = logs.filter((item) => item.status === "error").slice(0, 4);
  const modelCounts = Object.entries(
    logs.reduce((acc, item) => {
      const key = item.model || "未记录模型";
      acc[key] = (acc[key] || 0) + 1;
      return acc;
    }, {}),
  )
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4);

  return {
    totalAccounts,
    activeAccounts,
    disabledAccounts,
    accountSlotsUsed,
    accountSlotsTotal,
    saturatedAccounts,
    totalUsers,
    activeUsers,
    expiredUsers,
    disabledUsers,
    expiringUsers,
    userInflight,
    userCapacity,
    totalInvites,
    activeInvites,
    inviteRemainingUses,
    exhaustedInvites,
    totalGenerations,
    successGenerations,
    errorGenerations,
    successRate,
    streamCount,
    avgDuration,
    logs24h,
    lastSuccess,
    lastError,
    busiestAccounts,
    recentErrors,
    modelCounts,
  };
}

function renderSyncState() {
  const adminBadge = $("#adminIdentityBadge");
  const systemBadge = $("#systemPulseBadge");
  const updatedBadge = $("#consoleUpdatedAt");
  if (adminBadge) {
    adminBadge.textContent = state.admin?.username ? `管理员 · ${state.admin.username}` : "管理员";
  }
  if (systemBadge) {
    systemBadge.className = "hero-pill hero-pill-accent";
    if (state.refreshing) {
      systemBadge.textContent = "同步中";
    } else if (state.sync.status === "partial") {
      systemBadge.textContent = `部分失败 ${state.sync.successCount}/${state.sync.totalCount}`;
      systemBadge.classList.add("hero-pill-danger");
    } else if (state.sync.status === "ready") {
      systemBadge.textContent = "已同步";
      systemBadge.classList.add("hero-pill-success");
    } else {
      systemBadge.textContent = "未同步";
    }
  }
  if (updatedBadge) {
    updatedBadge.textContent = state.lastUpdatedAt
      ? `更新于 ${fmtTime(state.lastUpdatedAt)}`
      : "未刷新";
  }
}

function renderHeroGlance(metrics) {
  const el = $("#heroGlance");
  if (!el) return;
  el.innerHTML = `
    <article class="glance-card">
      <p class="glance-label">账号池容量</p>
      <strong class="glance-value">${fmtNumber(metrics.accountSlotsUsed)} / ${fmtNumber(metrics.accountSlotsTotal || 0)}</strong>
      <span class="glance-meta">${fmtNumber(metrics.activeAccounts)} 个启用账号，${fmtNumber(metrics.saturatedAccounts)} 个已打满</span>
    </article>
    <article class="glance-card">
      <p class="glance-label">用户活跃度</p>
      <strong class="glance-value">${fmtNumber(metrics.activeUsers)} / ${fmtNumber(metrics.totalUsers)}</strong>
      <span class="glance-meta">${fmtNumber(metrics.expiringUsers)} 个 7 天内到期，${fmtNumber(metrics.disabledUsers)} 个停用</span>
    </article>
    <article class="glance-card">
      <p class="glance-label">邀请码库存</p>
      <strong class="glance-value">${fmtNumber(metrics.activeInvites)} / ${fmtNumber(metrics.totalInvites)}</strong>
      <span class="glance-meta">剩余可用次数 ${fmtNumber(metrics.inviteRemainingUses)}</span>
    </article>
    <article class="glance-card">
      <p class="glance-label">生成质量</p>
      <strong class="glance-value">${metrics.successRate.toFixed(1)}%</strong>
      <span class="glance-meta">成功 ${fmtNumber(metrics.successGenerations)}，失败 ${fmtNumber(metrics.errorGenerations)}</span>
    </article>
  `;
}

function renderOverview() {
  const el = $("#overview");
  const metaEl = $("#overviewMeta");
  if (!el) return;
  if (!state.overview) {
    el.innerHTML = `
      <div class="stat-card stat-card-empty">
        <p class="stat-label">总览</p>
        <div class="stat-value">—</div>
        <p class="stat-meta">等待数据</p>
      </div>
    `;
    if (metaEl) metaEl.textContent = "总览接口尚未返回";
    return;
  }

  const metrics = deriveMetrics();
  el.innerHTML = `
    <div class="stat-card">
      <p class="stat-label">账号池</p>
      <div class="stat-value">${fmtNumber(metrics.activeAccounts)}<span class="stat-value-sub">/ ${fmtNumber(metrics.totalAccounts)}</span></div>
      <p class="stat-meta">启用账号占比 ${metrics.totalAccounts ? Math.round((metrics.activeAccounts / metrics.totalAccounts) * 100) : 0}%</p>
    </div>
    <div class="stat-card">
      <p class="stat-label">并发槽位</p>
      <div class="stat-value">${fmtNumber(metrics.accountSlotsUsed)}<span class="stat-value-sub">/ ${fmtNumber(metrics.accountSlotsTotal || 0)}</span></div>
      <p class="stat-meta">当前账号池在途请求 / 总容量</p>
    </div>
    <div class="stat-card">
      <p class="stat-label">用户</p>
      <div class="stat-value">${fmtNumber(metrics.activeUsers)}<span class="stat-value-sub">/ ${fmtNumber(metrics.totalUsers)}</span></div>
      <p class="stat-meta">${fmtNumber(metrics.expiredUsers)} 个已过期，${fmtNumber(metrics.disabledUsers)} 个停用</p>
    </div>
    <div class="stat-card">
      <p class="stat-label">邀请码</p>
      <div class="stat-value">${fmtNumber(metrics.activeInvites)}<span class="stat-value-sub">/ ${fmtNumber(metrics.totalInvites)}</span></div>
      <p class="stat-meta">剩余可用次数 ${fmtNumber(metrics.inviteRemainingUses)}</p>
    </div>
    <div class="stat-card">
      <p class="stat-label">成功率</p>
      <div class="stat-value"><span class="stat-success">${metrics.successRate.toFixed(1)}%</span></div>
      <p class="stat-meta">成功 ${fmtNumber(metrics.successGenerations)} / 失败 ${fmtNumber(metrics.errorGenerations)}</p>
    </div>
    <div class="stat-card">
      <p class="stat-label">近 24h</p>
      <div class="stat-value">${fmtNumber(metrics.logs24h.length)}<span class="stat-value-sub">/ ${fmtNumber(metrics.totalGenerations)}</span></div>
      <p class="stat-meta">平均耗时 ${fmtDuration(metrics.avgDuration)}，流式 ${fmtNumber(metrics.streamCount)}</p>
    </div>
  `;

  if (metaEl) {
    metaEl.textContent = state.lastUpdatedAt
      ? `上次刷新 ${fmtTime(state.lastUpdatedAt)} · ${state.sync.successCount || 0}/${state.sync.totalCount || 0} 数据块成功`
      : "等待首次刷新";
  }
}

function renderNavBadges(metrics) {
  const totalBlocks = state.sync.totalCount || 0;
  const successBlocks = state.sync.successCount || 0;
  const userRiskCount = metrics.expiredUsers + metrics.expiringUsers;
  const hasSynced = !!state.lastUpdatedAt;

  if (!hasSynced && state.refreshing) {
    setNavBadge("navBadgeOverview", "…", "warning");
    setNavBadge("navBadgeAccounts", "…", "warning");
    setNavBadge("navBadgeUsers", "…", "warning");
    setNavBadge("navBadgeInvites", "…", "warning");
    setNavBadge("navBadgeLogs", "…", "warning");
    return;
  }

  setNavBadge(
    "navBadgeOverview",
    totalBlocks ? `${successBlocks}/${totalBlocks}` : "—",
    state.refreshing ? "warning" : state.sync.status === "partial" ? "danger" : state.sync.status === "ready" ? "success" : "",
  );
  setNavBadge(
    "navBadgeAccounts",
    metrics.saturatedAccounts ? `${metrics.saturatedAccounts}险` : `${fmtNumber(metrics.activeAccounts)}`,
    metrics.saturatedAccounts ? "danger" : metrics.activeAccounts ? "success" : "",
  );
  setNavBadge(
    "navBadgeUsers",
    userRiskCount ? `${userRiskCount}险` : `${fmtNumber(metrics.activeUsers)}`,
    userRiskCount ? "warning" : metrics.activeUsers ? "success" : "",
  );
  setNavBadge(
    "navBadgeInvites",
    metrics.activeInvites ? `${fmtNumber(metrics.activeInvites)}` : "0",
    metrics.activeInvites ? "success" : "warning",
  );
  setNavBadge(
    "navBadgeLogs",
    metrics.errorGenerations ? `${fmtNumber(metrics.errorGenerations)}错` : `${fmtNumber(metrics.successGenerations)}`,
    metrics.errorGenerations ? "danger" : metrics.successGenerations ? "success" : "",
  );
}

function buildPriorityItems(metrics) {
  const items = [];
  const userRiskCount = metrics.expiredUsers + metrics.expiringUsers;
  const lowInventory = metrics.activeInvites === 0 || metrics.inviteRemainingUses <= Math.max(3, metrics.activeUsers);
  const hasSynced = !!state.lastUpdatedAt;

  if (!hasSynced && state.refreshing) {
    return [
      {
        tone: "warning",
        label: "Data Sync",
        title: "控制台正在拉取第一轮数据",
        detail: `已完成 ${state.sync.successCount || 0}/${state.sync.totalCount || 0} 个数据块，请稍候。`,
        actionLabel: "查看总览",
        href: "overviewSection",
      },
      {
        tone: "success",
        label: "Accounts",
        title: "账号池状态即将就绪",
        detail: "同步完成后，这里会优先提示负载、停用和打满账号。",
        actionLabel: "等待同步",
        href: "accountsSection",
      },
      {
        tone: "success",
        label: "Users",
        title: "用户侧风险稍后呈现",
        detail: "用户到期、停用与登录状态会在首轮同步后汇总到这里。",
        actionLabel: "等待同步",
        href: "usersSection",
      },
      {
        tone: "success",
        label: "Logs",
        title: "生成日志正在载入",
        detail: "错误回放、模型分布和成功率会在日志返回后展示。",
        actionLabel: "等待同步",
        href: "logsSection",
      },
    ];
  }

  if (state.refreshing) {
    items.push({
      tone: "warning",
      label: "Data Sync",
      title: "控制台正在刷新数据",
      detail: `当前已完成 ${state.sync.successCount || 0}/${state.sync.totalCount || 0} 个数据块。`,
      actionLabel: "查看总览",
      href: "overviewSection",
    });
  } else if (state.sync.status === "partial") {
    items.push({
      tone: "danger",
      label: "Sync Risk",
      title: "本轮同步存在失败的数据块",
      detail: `成功 ${state.sync.successCount || 0} / ${state.sync.totalCount || 0}，建议先检查失败分区。`,
      actionLabel: "回到总览",
      href: "overviewSection",
    });
  }

  if (metrics.saturatedAccounts > 0) {
    items.push({
      tone: "danger",
      label: "Accounts",
      title: `${fmtNumber(metrics.saturatedAccounts)} 个账号已打满`,
      detail: `当前账号池并发 ${fmtNumber(metrics.accountSlotsUsed)} / ${fmtNumber(metrics.accountSlotsTotal || 0)}，需要尽快扩容或停流。`,
      actionLabel: "查看账号池",
      href: "accountsSection",
    });
  }

  if (userRiskCount > 0) {
    items.push({
      tone: "warning",
      label: "Users",
      title: `${fmtNumber(userRiskCount)} 个用户需要关注`,
      detail: `${fmtNumber(metrics.expiredUsers)} 个已过期，${fmtNumber(metrics.expiringUsers)} 个 7 天内到期。`,
      actionLabel: "处理用户权限",
      href: "usersSection",
    });
  }

  if (metrics.errorGenerations > 0) {
    items.push({
      tone: "danger",
      label: "Generation Logs",
      title: `最近日志里有 ${fmtNumber(metrics.errorGenerations)} 条失败`,
      detail: metrics.lastError
        ? `最近一次失败发生在 ${fmtRelativeTime(metrics.lastError.timestamp)}。`
        : "建议先检查错误详情与上游返回。",
      actionLabel: "查看错误日志",
      href: "logsSection",
    });
  }

  if (lowInventory) {
    items.push({
      tone: "warning",
      label: "Invites",
      title: metrics.activeInvites ? "邀请码库存偏低" : "当前没有可用邀请码",
      detail: `可用邀请码 ${fmtNumber(metrics.activeInvites)} 个，剩余可用次数 ${fmtNumber(metrics.inviteRemainingUses)}。`,
      actionLabel: "补充邀请码",
      href: "invitesSection",
    });
  }

  const backups = [
    {
      tone: "success",
      label: "Accounts",
      title: "账号池当前处于可控负载",
      detail: `${fmtNumber(metrics.activeAccounts)} 个启用账号，当前并发 ${fmtNumber(metrics.accountSlotsUsed)} / ${fmtNumber(metrics.accountSlotsTotal || 0)}。`,
      actionLabel: "查看账号池",
      href: "accountsSection",
    },
    {
      tone: "success",
      label: "Users",
      title: "用户侧目前没有明显生命周期风险",
      detail: `${fmtNumber(metrics.activeUsers)} 个可用用户，${fmtNumber(metrics.disabledUsers)} 个停用。`,
      actionLabel: "查看用户",
      href: "usersSection",
    },
    {
      tone: "success",
      label: "Logs",
      title: "生成链路整体稳定",
      detail: `当前成功率 ${metrics.successRate.toFixed(1)}%，平均耗时 ${fmtDuration(metrics.avgDuration)}。`,
      actionLabel: "查看日志",
      href: "logsSection",
    },
    {
      tone: "success",
      label: "Invites",
      title: "邀请码库存充足",
      detail: `当前可用邀请码 ${fmtNumber(metrics.activeInvites)} 个，剩余次数 ${fmtNumber(metrics.inviteRemainingUses)}。`,
      actionLabel: "查看邀请码",
      href: "invitesSection",
    },
  ];

  for (const item of backups) {
    if (items.length >= 4) break;
    if (!items.some((entry) => entry.label === item.label)) items.push(item);
  }

  return items.slice(0, 4);
}

function renderPriorityBoard(metrics) {
  const el = $("#priorityBoard");
  const metaEl = $("#workspaceFocusMeta");
  if (!el) return;

  const items = buildPriorityItems(metrics);
  el.innerHTML = items
    .map(
      (item) => `
        <a href="#${item.href}" class="priority-card is-${item.tone}">
          <span class="priority-kicker">${escapeHtml(item.label)}</span>
          <strong class="priority-title">${escapeHtml(item.title)}</strong>
          <p class="priority-detail">${escapeHtml(item.detail)}</p>
          <span class="priority-action">${escapeHtml(item.actionLabel)}</span>
        </a>
      `,
    )
    .join("");

  if (metaEl) {
    const hasDanger = items.some((item) => item.tone === "danger");
    const hasWarning = items.some((item) => item.tone === "warning");
    metaEl.textContent = hasDanger ? "需优先处理" : hasWarning ? "建议跟进" : "运行平稳";
  }
}

function renderSectionStatusBoard(metrics) {
  const el = $("#sectionStatusBoard");
  const metaEl = $("#workspaceActionMeta");
  if (!el) return;
  const hasSynced = !!state.lastUpdatedAt;

  const statuses = [
    {
      href: "overviewSection",
      label: "总览",
      value: !hasSynced && state.refreshing ? "加载中" : state.refreshing ? "同步中" : state.sync.status === "partial" ? "部分失败" : state.sync.status === "ready" ? "已同步" : "待刷新",
      note: `${state.sync.successCount || 0}/${state.sync.totalCount || 0} 数据块`,
      tone: state.refreshing ? "warning" : state.sync.status === "partial" ? "danger" : state.sync.status === "ready" ? "success" : "",
    },
    {
      href: "accountsSection",
      label: "账号池",
      value: !hasSynced && state.refreshing ? "载入中" : `${fmtNumber(metrics.activeAccounts)} / ${fmtNumber(metrics.totalAccounts)}`,
      note: !hasSynced && state.refreshing ? "正在同步账号与并发状态" : metrics.saturatedAccounts ? `${fmtNumber(metrics.saturatedAccounts)} 个已打满` : "当前没有满载账号",
      tone: !hasSynced && state.refreshing ? "warning" : metrics.saturatedAccounts ? "danger" : "success",
    },
    {
      href: "usersSection",
      label: "用户",
      value: !hasSynced && state.refreshing ? "载入中" : `${fmtNumber(metrics.activeUsers)} / ${fmtNumber(metrics.totalUsers)}`,
      note:
        !hasSynced && state.refreshing
          ? "正在同步用户生命周期状态"
          : metrics.expiredUsers + metrics.expiringUsers
          ? `${fmtNumber(metrics.expiredUsers + metrics.expiringUsers)} 个到期风险`
          : "生命周期状态稳定",
      tone: !hasSynced && state.refreshing ? "warning" : metrics.expiredUsers + metrics.expiringUsers ? "warning" : "success",
    },
    {
      href: "invitesSection",
      label: "邀请码",
      value: !hasSynced && state.refreshing ? "载入中" : `${fmtNumber(metrics.activeInvites)} 个`,
      note: !hasSynced && state.refreshing ? "正在同步库存与权益配置" : `剩余可用次数 ${fmtNumber(metrics.inviteRemainingUses)}`,
      tone: !hasSynced && state.refreshing ? "warning" : metrics.activeInvites ? "success" : "warning",
    },
    {
      href: "logsSection",
      label: "日志",
      value: !hasSynced && state.refreshing ? "载入中" : `${metrics.successRate.toFixed(1)}%`,
      note: !hasSynced && state.refreshing ? "正在拉取最近生成记录" : metrics.errorGenerations ? `${fmtNumber(metrics.errorGenerations)} 条失败` : "当前无失败日志",
      tone: !hasSynced && state.refreshing ? "warning" : metrics.errorGenerations ? "danger" : "success",
    },
  ];

  el.innerHTML = statuses
    .map(
      (item) => `
        <a href="#${item.href}" class="section-status-item is-${item.tone}">
          <div>
            <span class="section-status-label">${escapeHtml(item.label)}</span>
            <strong class="section-status-value">${escapeHtml(item.value)}</strong>
          </div>
          <span class="section-status-note">${escapeHtml(item.note)}</span>
        </a>
      `,
    )
    .join("");

  if (metaEl) {
    metaEl.textContent = state.lastUpdatedAt ? `更新于 ${fmtTime(state.lastUpdatedAt)}` : `${statuses.length} 个分区`;
  }
}

function renderInsightCard(el, title, subtitle, bodyHtml) {
  if (!el) return;
  el.innerHTML = `
    <div class="insight-head">
      <div>
        <p class="panel-kicker">${escapeHtml(subtitle)}</p>
        <h3>${escapeHtml(title)}</h3>
      </div>
    </div>
    ${bodyHtml}
  `;
}

// ==================== 运行状态（流量守卫） ====================

function formatOverviewUptime(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = Math.floor(value / 60);
  const secs = value % 60;
  return `${minutes}分${secs}秒`;
}

function renderOverviewUptime() {
  const el = $("#overviewUptime");
  if (!el) return;
  const seconds = state.runtimeStatus?.uptime_seconds;
  el.textContent = Number.isFinite(Number(seconds)) ? `已运行: ${formatOverviewUptime(seconds)}` : "已运行: --";
}

function renderRuntimeStatusCard() {
  const el = $("#runtimeStatusPanel");
  if (!el) return;
  renderOverviewUptime();
  const status = state.runtimeStatus;
  if (!status) {
    renderInsightCard(el, "运行状态", "Runtime", `<p class="insight-empty muted">加载中…</p>`);
    return;
  }

  const gate = status.guard?.global_gate;
  const breaker = status.guard?.circuit_breaker;
  const rpm = status.guard?.user_rpm;
  const cooldowns = status.account_cooldowns || [];

  const gateCell = gate?.enabled
    ? `<strong class="mono">${fmtNumber(gate.in_flight)} / ${fmtNumber(gate.max_concurrent)}</strong>`
    : `<strong>未启用</strong>`;

  const breakerCell = !breaker?.enabled
    ? "<strong>未启用</strong>"
    : breaker.is_open
      ? `<strong class="runtime-danger">已断开 ${Math.ceil(breaker.remaining_seconds)}s</strong>`
      : `<strong>正常</strong>`;

  const rpmCell = rpm?.enabled
    ? `<strong class="mono">${fmtNumber(rpm.limit)}/分钟${rpm.rejected ? `（拒 ${fmtNumber(rpm.rejected)}）` : ""}</strong>`
    : "<strong>未启用</strong>";

  const cooldownLines = cooldowns.length
    ? cooldowns
        .slice(0, 4)
        .map(
          (item) =>
            `<li><span class="mono">${escapeHtml(item.name || item.account_id)}</span> · ${Math.ceil(item.remaining_seconds)}s</li>`,
        )
        .join("") +
      (cooldowns.length > 4 ? `<li class="muted">… 共 ${cooldowns.length} 个</li>` : "")
    : `<li class="muted">无</li>`;

  renderInsightCard(
    el,
    "运行状态",
    "Runtime",
    `
      <div class="insight-metrics runtime-grid">
        <div class="insight-metric">
          <span>全局在途${gate?.enabled && gate.rejected ? ` · 拒 ${fmtNumber(gate.rejected)}` : ""}</span>
          ${gateCell}
        </div>
        <div class="insight-metric">
          <span>上游熔断${breaker?.enabled && !breaker.is_open && breaker.consecutive_failures ? ` · 连败 ${breaker.consecutive_failures}/${breaker.failure_threshold}` : ""}</span>
          ${breakerCell}
        </div>
        <div class="insight-metric">
          <span>用户限速</span>
          ${rpmCell}
        </div>
      </div>
      <div class="runtime-cooldowns">
        <p class="runtime-cooldowns-title">冷却账号（${cooldowns.length}）</p>
        <ul>${cooldownLines}</ul>
      </div>
      ${
        breaker?.enabled
          ? `<div class="runtime-actions"><button id="resetBreakerBtn" class="btn btn-ghost" type="button" ${breaker.is_open ? "" : "disabled"}>复位熔断器</button></div>`
          : ""
      }
    `,
  );

  const resetBtn = $("#resetBreakerBtn");
  if (resetBtn) {
    resetBtn.addEventListener("click", async () => {
      try {
        await withBusyButton(resetBtn, "复位中…", async () =>
          api("/api/admin/circuit-breaker/reset", { method: "POST" })
        );
        await refreshRuntimeStatus();
        showToast("熔断器已复位", "success");
      } catch (err) {
        showToast(`复位失败：${err.message}`, "error");
      }
    });
  }
}

let _runtimeStatusTimer = null;

async function refreshRuntimeStatus() {
  try {
    state.runtimeStatus = await api("/api/admin/runtime-status");
    renderRuntimeStatusCard();
    return true;
  } catch (err) {
    const el = $("#runtimeStatusPanel");
    if (el) renderInsightCard(el, "运行状态", "Runtime", `<p class="insight-empty muted">加载失败：${escapeHtml(err.message)}</p>`);
    return false;
  }
}

function startRuntimeStatusPolling() {
  stopRuntimeStatusPolling();
  _runtimeStatusTimer = window.setInterval(() => {
    if (document.hidden) return; // 页面不可见时暂停轮询
    refreshRuntimeStatus();
  }, 5000);
}

function stopRuntimeStatusPolling() {
  if (_runtimeStatusTimer) {
    window.clearInterval(_runtimeStatusTimer);
    _runtimeStatusTimer = null;
  }
}

function renderInsightPanels() {
  const metrics = deriveMetrics();

  renderHeroGlance(metrics);
  renderSyncState();
  renderOverview();
  renderOverviewUptime();
  renderNavBadges(metrics);
  renderPriorityBoard(metrics);
  renderSectionStatusBoard(metrics);
  renderRuntimeStatusCard();

  renderInsightCard(
    $("#opsSnapshot"),
    "运行快照",
    "Operations",
    `
      <div class="insight-metrics">
        <div class="insight-metric">
          <span>最近成功</span>
          <strong>${metrics.lastSuccess ? fmtRelativeTime(metrics.lastSuccess.timestamp) : "暂无"}</strong>
        </div>
        <div class="insight-metric">
          <span>最近失败</span>
          <strong>${metrics.lastError ? fmtRelativeTime(metrics.lastError.timestamp) : "暂无"}</strong>
        </div>
        <div class="insight-metric">
          <span>流式占比</span>
          <strong>${metrics.totalGenerations ? ((metrics.streamCount / metrics.totalGenerations) * 100).toFixed(1) : "0.0"}%</strong>
        </div>
        <div class="insight-metric">
          <span>用户并发</span>
          <strong>${fmtNumber(metrics.userInflight)} / ${fmtNumber(metrics.userCapacity || 0)}</strong>
        </div>
      </div>
    `,
  );

  renderInsightCard(
    $("#accountHealthPanel"),
    "账号健康",
    "Accounts",
    metrics.busiestAccounts.length
      ? `
          <div class="insight-list">
            ${metrics.busiestAccounts
              .map((account) => {
                const load = computeAccountLoadMeta(account);
                return `
                  <div class="insight-item">
                    <div>
                      <strong>${escapeHtml(shortAccount(account.name))}</strong>
                      <span>${escapeHtml(account.name)}</span>
                    </div>
                    <div class="insight-item-side">
                      ${badgeHtml(load.label, load.variant)}
                      <span class="mono">${account.in_flight || 0}/${account.max_inflight || 0}</span>
                    </div>
                  </div>
                `;
              })
              .join("")}
          </div>
        `
      : '<div class="insight-empty">暂无账号数据。</div>',
  );

  renderInsightCard(
    $("#logHealthPanel"),
    "异常与模型分布",
    "Logs",
    `
      <div class="insight-split">
        <div>
          <p class="insight-caption">最近错误</p>
          ${
            metrics.recentErrors.length
              ? metrics.recentErrors
                  .map(
                    (log) => `
                      <div class="insight-item compact">
                        <div>
                          <strong>${escapeHtml(log.model || humanMode(log.mode))}</strong>
                          <span>${escapeHtml(truncateText(log.error_message || "未知错误", 42))}</span>
                        </div>
                        <span>${escapeHtml(fmtRelativeTime(log.timestamp))}</span>
                      </div>
                    `,
                  )
                  .join("")
              : '<div class="insight-empty">最近没有错误记录。</div>'
          }
        </div>
        <div>
          <p class="insight-caption">常用模型</p>
          ${
            metrics.modelCounts.length
              ? metrics.modelCounts
                  .map(
                    ([model, count]) => `
                      <div class="insight-item compact">
                        <div>
                          <strong>${escapeHtml(model)}</strong>
                          <span>${escapeHtml(count)} 次调用</span>
                        </div>
                      </div>
                    `,
                  )
                  .join("")
              : '<div class="insight-empty">暂无模型分布数据。</div>'
          }
        </div>
      </div>
    `,
  );
}

function accountFilterChips() {
  const chips = [];
  if (state.filters.accounts.query.trim()) {
    chips.push(filterChipHtml("搜索", state.filters.accounts.query.trim()));
  }
  if (state.filters.accounts.status !== "all") {
    chips.push(
      filterChipHtml("状态", state.filters.accounts.status === "active" ? "仅启用" : "仅停用"),
    );
  }
  if (state.filters.accounts.load !== "all") {
    const loadLabelMap = {
      saturated: "已打满",
      busy: "处理中",
      idle: "空闲",
    };
    chips.push(filterChipHtml("负载", loadLabelMap[state.filters.accounts.load] || state.filters.accounts.load));
  }
  return chips;
}

function userFilterChips() {
  const chips = [];
  if (state.filters.users.query.trim()) {
    chips.push(filterChipHtml("搜索", state.filters.users.query.trim()));
  }
  if (state.filters.users.status !== "all") {
    const statusLabelMap = {
      active: "可用中",
      disabled: "已停用",
      expired: "已过期",
    };
    chips.push(filterChipHtml("状态", statusLabelMap[state.filters.users.status] || state.filters.users.status));
  }
  if (state.filters.users.lifecycle !== "all") {
    const lifecycleLabelMap = {
      expiring: "7 天内到期",
      permanent: "永久有效",
      limited: "有限期",
    };
    chips.push(
      filterChipHtml("期限", lifecycleLabelMap[state.filters.users.lifecycle] || state.filters.users.lifecycle),
    );
  }
  return chips;
}

function inviteFilterChips() {
  const chips = [];
  if (state.filters.invites.query.trim()) {
    chips.push(filterChipHtml("搜索", state.filters.invites.query.trim()));
  }
  if (state.filters.invites.status !== "all") {
    const statusLabelMap = {
      active: "可用中",
      revoked: "已撤销",
      expired: "已过期",
      exhausted: "已用尽",
    };
    chips.push(filterChipHtml("状态", statusLabelMap[state.filters.invites.status] || state.filters.invites.status));
  }
  return chips;
}

function logFilterChips() {
  const chips = [];
  if (state.filters.logs.query.trim()) {
    chips.push(filterChipHtml("搜索", state.filters.logs.query.trim()));
  }
  if (state.filters.logs.status !== "all") {
    chips.push(filterChipHtml("状态", state.filters.logs.status === "success" ? "成功" : "失败"));
  }
  if (state.filters.logs.mode !== "all") {
    chips.push(filterChipHtml("模式", state.filters.logs.mode === "img2img" ? "图生图" : "文生图"));
  }
  return chips;
}

function filteredAccounts() {
  const query = state.filters.accounts.query.trim().toLowerCase();
  return state.accounts.filter((account) => {
    const matchesQuery =
      !query ||
      [account.name, account.org_id, account.flow_id]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(query));
    const matchesStatus =
      state.filters.accounts.status === "all" || account.status === state.filters.accounts.status;
    const loadMeta = computeAccountLoadMeta(account);
    const matchesLoad =
      state.filters.accounts.load === "all" ||
      (state.filters.accounts.load === "busy" && loadMeta.ratio > 0 && loadMeta.ratio < 1) ||
      (state.filters.accounts.load === "saturated" && loadMeta.ratio >= 1) ||
      (state.filters.accounts.load === "idle" && loadMeta.ratio === 0 && account.status === "active");
    return matchesQuery && matchesStatus && matchesLoad;
  });
}

function renderAccountRow(account) {
  const load = computeAccountLoadMeta(account);
  const toggle = toggleActionMeta(account.status);
  return `
    <tr data-id="${account.id}">
      <td class="col-account-name" data-label="账号">
        <div class="entity-cell">
          <strong>${escapeHtml(account.name)}</strong>
          <span class="entity-meta">
            <span class="mono">${escapeHtml(shortAccount(account.name))}</span>
            <span>${account.private_api_key_set ? "已配 Private Key" : "无 Private Key"}</span>
          </span>
        </div>
      </td>
      <td class="col-account-org-flow" data-label="工作流">
        <div class="account-org-flow-stack">
          <code title="org_id">${escapeHtml(account.org_id)}</code>
          <code title="flow_id">${escapeHtml(account.flow_id)}</code>
        </div>
      </td>
      <td class="col-account-load" data-label="负载">
        <div class="metric-stack">
          <div class="metric-head">
            <strong class="mono">${fmtNumber(account.in_flight || 0)} / ${fmtNumber(account.max_inflight || 0)}</strong>
            ${badgeHtml(load.label, load.variant)}
            ${account.cooldown_seconds ? badgeHtml(`冷却中 ${Math.ceil(account.cooldown_seconds)}s`, "warning") : ""}
          </div>
          ${renderCapacityBar(account.in_flight || 0, account.max_inflight || 0)}
        </div>
      </td>
      <td class="col-account-status" data-label="状态">
        <div class="stack-cell account-status-stack">
          ${accountStatusBadge(account.status)}
        </div>
      </td>
      <td class="col-account-total" data-label="累计 / 最近使用">
        <div class="entity-cell">
          <strong class="mono">${fmtNumber(account.total_requests)}</strong>
          <span class="entity-meta">${escapeHtml(account.last_used_at ? `最近使用 ${fmtRelativeTime(account.last_used_at)}` : "尚未被使用")}</span>
        </div>
      </td>
      <td class="col-account-actions" data-label="操作">
        <div class="table-actions table-actions-grid">
          <button class="${toggle.className}" data-action="toggle-account" type="button">${toggle.label}</button>
          <button class="btn btn-ghost" data-action="test" type="button">测试</button>
          <button class="btn btn-ghost" data-action="edit" type="button">编辑</button>
          <button class="btn btn-danger" data-action="delete" type="button">删除</button>
        </div>
      </td>
    </tr>
  `;
}

function bindAccountActions(items) {
  $$("#accountsTable tbody tr").forEach((row) => {
    const id = row.dataset.id;
    const account = items.find((item) => item.id === id);
    if (!account) return;

    row.querySelector('[data-action="edit"]').addEventListener("click", () => openAccountModal(account));

    row.querySelector('[data-action="toggle-account"]').addEventListener("click", async (event) => {
      const toggle = toggleActionMeta(account.status);
      if (!confirm(`确认${toggle.label}账号 "${account.name}" ?`)) return;
      try {
        await withBusyButton(event.currentTarget, `${toggle.label}中…`, async () => {
          await api(`/api/admin/accounts/${id}`, {
            method: "PUT",
            body: JSON.stringify({ status: toggle.nextStatus }),
          });
          await Promise.all([refreshAccounts(), refreshOverview()]);
          renderInsightPanels();
        });
        showToast(`账号已${toggle.label === "启用" ? "启用" : "停用"}`, "success");
      } catch (err) {
        showToast(`${toggle.label}失败：${err.message}`, "error");
      }
    });

    row.querySelector('[data-action="delete"]').addEventListener("click", async (event) => {
      if (!confirm(`确认删除账号 "${account.name}" ?`)) return;
      try {
        await withBusyButton(event.currentTarget, "删除中…", async () => {
          await api(`/api/admin/accounts/${id}`, { method: "DELETE" });
          await Promise.all([refreshAccounts(), refreshOverview()]);
          renderInsightPanels();
        });
        showToast("账号已删除", "success");
      } catch (err) {
        showToast(`删除失败：${err.message}`, "error");
      }
    });

    row.querySelector('[data-action="test"]').addEventListener("click", async (event) => {
      try {
        await withBusyButton(event.currentTarget, "测试中…", async () => {
          const data = await api(`/api/admin/accounts/${id}/test`, { method: "POST" });
          if (data.ok) {
            showToast(`${account.name} 可用`, "success");
          } else {
            showToast(`${account.name} 测试失败：${data.status_code || "?"} ${data.message}`, "error");
          }
        });
      } catch (err) {
        showToast(`测试失败：${err.message}`, "error");
      }
    });
  });
}

function renderAccountsTable() {
  const tbody = $("#accountsTable tbody");
  const summary = $("#accountsSummary");
  const items = filteredAccounts();
  const active = state.accounts.filter((item) => item.status === "active").length;
  const inflight = sumBy(state.accounts, (item) => item.in_flight);
  const capacity = sumBy(state.accounts, (item) => item.max_inflight);
  renderToolbarMeta(
    "accountsFilterMeta",
    "账号",
    items.length,
    state.accounts.length,
    accountFilterChips(),
    "accounts",
    "支持搜索与筛选。",
  );
  if (summary) {
    const cooling = state.accounts.filter((item) => item.cooldown_seconds).length;
    summary.textContent = `共 ${fmtNumber(state.accounts.length)} 个账号，${fmtNumber(active)} 个启用；当前并发 ${fmtNumber(inflight)} / ${fmtNumber(capacity)}${cooling ? `；${fmtNumber(cooling)} 个冷却中` : ""}。`;
  }

  if (!state.accounts.length) {
    tbody.innerHTML = renderEmptyRow(6, "暂无账号。", "先新增账号或批量导入。");
    return;
  }
  if (!items.length) {
    tbody.innerHTML = renderEmptyRow(6, "没有匹配结果。", "尝试放宽筛选条件。");
    return;
  }
  tbody.innerHTML = items.map(renderAccountRow).join("");
  bindAccountActions(items);
}

async function refreshAccounts() {
  const tbody = $("#accountsTable tbody");
  tbody.innerHTML = renderEmptyRow(6, "账号池加载中…", "正在同步账号数据。");
  try {
    const data = await api("/api/admin/accounts");
    state.accounts = Array.isArray(data.items) ? data.items : [];
    renderAccountsTable();
    renderInsightPanels();
    return true;
  } catch (err) {
    state.accounts = [];
    renderToolbarMeta(
      "accountsFilterMeta",
      "账号",
      0,
      0,
      accountFilterChips(),
      "accounts",
      "支持搜索与筛选。",
    );
    tbody.innerHTML = renderErrorRow(6, err.message);
    const summary = $("#accountsSummary");
    if (summary) summary.textContent = `账号池加载失败：${err.message}`;
    renderInsightPanels();
    return false;
  }
}

async function bulkUpdateAccountStatus(button, status) {
  const label = status === "active" ? "全部启用" : "全部停用";
  const targets = state.accounts.filter((item) => item.status !== status);
  if (!state.accounts.length) {
    showToast("当前没有账号可操作", "info");
    return;
  }
  if (!targets.length) {
    showToast(status === "active" ? "所有账号已经启用" : "所有账号已经停用", "info");
    return;
  }
  if (!confirm(`确认${label}所有账号？`)) return;
  try {
    await withBusyButton(button, "处理中…", async () => {
      try {
        await api("/api/admin/accounts/bulk/status", {
          method: "POST",
          body: JSON.stringify({ status }),
        });
      } catch (err) {
        if (err.status !== 404 && err.status !== 405) throw err;
        const failures = [];
        for (const account of targets) {
          try {
            await api(`/api/admin/accounts/${account.id}`, {
              method: "PUT",
              body: JSON.stringify({ status }),
            });
          } catch (itemErr) {
            failures.push(`${account.name}: ${itemErr.message}`);
          }
        }
        if (failures.length) {
          const summary = failures.slice(0, 3).join("；");
          throw new Error(
            failures.length > 3 ? `${summary}；另外还有 ${failures.length - 3} 个失败` : summary,
          );
        }
      }
      await Promise.all([refreshAccounts(), refreshOverview()]);
      renderInsightPanels();
    });
    showToast(`${label}完成`, "success");
  } catch (err) {
    showToast(`${label}失败：${err.message}`, "error");
  }
}

async function deleteAllAccounts(button) {
  if (!state.accounts.length) {
    showToast("当前没有账号可删除", "info");
    return;
  }
  if (!confirm("确认删除所有账号？此操作不可恢复。")) return;
  try {
    await withBusyButton(button, "删除中…", async () => {
      try {
        await api("/api/admin/accounts", { method: "DELETE" });
      } catch (err) {
        if (err.status !== 404 && err.status !== 405) throw err;
        const failures = [];
        for (const account of state.accounts) {
          try {
            await api(`/api/admin/accounts/${account.id}`, { method: "DELETE" });
          } catch (itemErr) {
            failures.push(`${account.name}: ${itemErr.message}`);
          }
        }
        if (failures.length) {
          const summary = failures.slice(0, 3).join("；");
          throw new Error(
            failures.length > 3 ? `${summary}；另外还有 ${failures.length - 3} 个失败` : summary,
          );
        }
      }
      await Promise.all([refreshAccounts(), refreshOverview()]);
      renderInsightPanels();
    });
    showToast("全部账号已删除", "success");
  } catch (err) {
    showToast(`全部删除失败：${err.message}`, "error");
  }
}

function filteredUsers() {
  const query = state.filters.users.query.trim().toLowerCase();
  return state.users.filter((user) => {
    const lifecycle = computeUserLifecycle(user);
    const matchesQuery = !query || String(user.username || "").toLowerCase().includes(query);
    const matchesStatus =
      state.filters.users.status === "all" || lifecycle === state.filters.users.status;
    const matchesLifecycle =
      state.filters.users.lifecycle === "all" ||
      (state.filters.users.lifecycle === "expiring" && isExpiringSoon(user)) ||
      (state.filters.users.lifecycle === "permanent" && !user.expires_at) ||
      (state.filters.users.lifecycle === "limited" && !!user.expires_at);
    return matchesQuery && matchesStatus && matchesLifecycle;
  });
}

function renderUserExpiry(user) {
  if (!user?.expires_at) return '<span class="table-note">永久有效</span>';
  const soon = isExpiringSoon(user);
  return `
    <div class="stack-cell">
      <span class="mono">${escapeHtml(fmtDate(user.expires_at))}</span>
      <span class="table-note ${soon ? "table-note-warning" : ""}">${escapeHtml(fmtRelativeTime(user.expires_at))}</span>
    </div>
  `;
}

function renderUserRow(user) {
  const lifecycle = computeUserLifecycle(user);
  const toggle = toggleActionMeta(user.status);
  return `
    <tr data-id="${user.id}">
      <td class="col-user-name" data-label="用户">
        <div class="entity-cell">
          <strong>${escapeHtml(user.username)}</strong>
          <span class="entity-meta">${escapeHtml(user.last_login_at ? `最近登录 ${fmtRelativeTime(user.last_login_at)}` : "尚未登录")}</span>
        </div>
      </td>
      <td class="col-user-status" data-label="状态">
        <div class="stack-cell">
          ${userStatusBadge(lifecycle)}
          <span class="table-note">${escapeHtml(user.invite_code_id ? "邀请码注册" : "手动创建")}</span>
        </div>
      </td>
      <td class="col-user-quota" data-label="额度 / 并发">
        <div class="metric-stack">
          <div class="metric-head">
            <strong class="mono">${escapeHtml(fmtQuota(user.daily_used || 0, user.daily_quota || 0))}</strong>
            <span class="mono">并发 ${fmtNumber(user.in_flight || 0)}/${fmtNumber(user.max_inflight || 0)}</span>
          </div>
          ${renderCapacityBar(user.in_flight || 0, user.max_inflight || 0)}
        </div>
      </td>
      <td class="col-user-expiry" data-label="期限">${renderUserExpiry(user)}</td>
      <td class="col-user-total" data-label="累计 / 最近登录">
        <div class="entity-cell">
          <strong class="mono">${fmtNumber(user.total_requests || 0)}</strong>
          <span class="entity-meta">${escapeHtml(user.last_used_at ? `最近生成 ${fmtRelativeTime(user.last_used_at)}` : "尚无生成记录")}</span>
        </div>
      </td>
      <td class="col-user-actions" data-label="操作">
        <div class="table-actions">
          <button class="${toggle.className}" data-action="toggle-user" type="button">${toggle.label}</button>
          <button class="btn btn-ghost" data-action="edit-user" type="button">编辑</button>
          <button class="btn btn-danger" data-action="delete-user" type="button">删除</button>
        </div>
      </td>
    </tr>
  `;
}

function bindUserActions(items) {
  $$("#usersTable tbody tr").forEach((row) => {
    const id = row.dataset.id;
    const user = items.find((item) => item.id === id);
    if (!user) return;

    row.querySelector('[data-action="toggle-user"]').addEventListener("click", async (event) => {
      const toggle = toggleActionMeta(user.status);
      if (!confirm(`确认${toggle.label}用户 "${user.username}" ?`)) return;
      try {
        await withBusyButton(event.currentTarget, `${toggle.label}中…`, async () => {
          await api(`/api/admin/users/${id}`, {
            method: "PUT",
            body: JSON.stringify({ status: toggle.nextStatus }),
          });
          await Promise.all([refreshUsers(), refreshOverview()]);
          renderInsightPanels();
        });
        showToast(`用户已${toggle.label === "启用" ? "启用" : "停用"}`, "success");
      } catch (err) {
        showToast(`${toggle.label}失败：${err.message}`, "error");
      }
    });

    row.querySelector('[data-action="edit-user"]').addEventListener("click", () => openUserModal(user));

    row.querySelector('[data-action="delete-user"]').addEventListener("click", async (event) => {
      if (!confirm(`确认删除用户 "${user.username}" ? 此操作不可恢复。`)) return;
      try {
        await withBusyButton(event.currentTarget, "删除中…", async () => {
          await api(`/api/admin/users/${id}`, { method: "DELETE" });
          await Promise.all([refreshUsers(), refreshOverview()]);
          renderInsightPanels();
        });
        showToast("用户已删除", "success");
      } catch (err) {
        showToast(
          isMissingDeleteRouteError(err)
            ? "删除失败：当前后端实例还没加载用户删除接口，请先重启 st-imagen 服务。"
            : `删除失败：${err.message}`,
          "error",
        );
      }
    });
  });
}

function renderUsersTable() {
  const tbody = $("#usersTable tbody");
  const summary = $("#usersSummary");
  const items = filteredUsers();
  const active = state.users.filter((item) => computeUserLifecycle(item) === "active").length;
  const expiring = state.users.filter((item) => isExpiringSoon(item)).length;
  renderToolbarMeta(
    "usersFilterMeta",
    "用户",
    items.length,
    state.users.length,
    userFilterChips(),
    "users",
    "支持搜索与筛选。",
  );
  if (summary) {
    summary.textContent = `共 ${fmtNumber(state.users.length)} 个用户，${fmtNumber(active)} 个可用；${fmtNumber(expiring)} 个 7 天内到期。`;
  }

  if (!state.users.length) {
    tbody.innerHTML = renderEmptyRow(6, "暂无用户。", "可手动创建用户，或先发放邀请码。");
    return;
  }
  if (!items.length) {
    tbody.innerHTML = renderEmptyRow(6, "没有匹配结果。", "尝试放宽筛选条件。");
    return;
  }
  tbody.innerHTML = items.map(renderUserRow).join("");
  bindUserActions(items);
}

async function refreshUsers() {
  const tbody = $("#usersTable tbody");
  tbody.innerHTML = renderEmptyRow(6, "用户列表加载中…", "正在同步用户数据。");
  try {
    const data = await api("/api/admin/users");
    state.users = Array.isArray(data.items) ? data.items : [];
    renderUsersTable();
    renderInsightPanels();
    return true;
  } catch (err) {
    state.users = [];
    renderToolbarMeta(
      "usersFilterMeta",
      "用户",
      0,
      0,
      userFilterChips(),
      "users",
      "支持搜索与筛选。",
    );
    tbody.innerHTML = renderErrorRow(6, err.message);
    const summary = $("#usersSummary");
    if (summary) summary.textContent = `用户列表加载失败：${err.message}`;
    renderInsightPanels();
    return false;
  }
}

async function deleteAllUsers(button) {
  if (!state.users.length) {
    showToast("当前没有用户可删除", "info");
    return;
  }
  if (!confirm("确认删除所有用户？此操作会同时清空用户会话，且不可恢复。")) return;
  try {
    await withBusyButton(button, "删除中…", async () => {
      try {
        await api("/api/admin/users", { method: "DELETE" });
      } catch (err) {
        if (err.status !== 404 && err.status !== 405) throw err;
        if (isMissingDeleteRouteError(err)) {
          throw new Error("当前后端实例还没加载用户批量删除接口，请先重启 st-imagen 服务。");
        }
        const failures = [];
        for (const user of state.users) {
          try {
            await api(`/api/admin/users/${user.id}`, { method: "DELETE" });
          } catch (itemErr) {
            if (isMissingDeleteRouteError(itemErr)) {
              throw new Error("当前后端实例还没加载用户删除接口，请先重启 st-imagen 服务。");
            }
            failures.push(`${user.username}: ${itemErr.message}`);
          }
        }
        if (failures.length) {
          const summary = failures.slice(0, 3).join("；");
          throw new Error(
            failures.length > 3 ? `${summary}；另外还有 ${failures.length - 3} 个失败` : summary,
          );
        }
      }
      await Promise.all([refreshUsers(), refreshOverview()]);
      renderInsightPanels();
    });
    showToast("全部用户已删除", "success");
  } catch (err) {
    showToast(`全部删除失败：${err.message}`, "error");
  }
}

function filteredInvites() {
  const query = state.filters.invites.query.trim().toLowerCase();
  return state.invites.filter((invite) => {
    const matchesQuery =
      !query ||
      [invite.code_prefix, invite.code_suffix, invite.note]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(query));
    const matchesStatus =
      state.filters.invites.status === "all" || invite.status === state.filters.invites.status;
    return matchesQuery && matchesStatus;
  });
}

function maskInviteCode(invite) {
  const prefix = String(invite.code_prefix || "").trim();
  const suffix = String(invite.code_suffix || "").trim();
  if (prefix && suffix) return `${prefix}..${suffix.slice(-4)}`;
  return prefix || "—";
}

function renderInviteRow(invite) {
  const remaining = Math.max(0, Number(invite.max_uses || 0) - Number(invite.used_count || 0));
  const revokeButton =
    invite.status === "active"
      ? '<button class="btn btn-danger" data-action="revoke-invite" type="button">撤销</button>'
      : '<button class="btn btn-ghost" type="button" disabled>已结束</button>';
  return `
    <tr data-id="${invite.id}">
      <td class="col-invite-code" data-label="邀请码">
        <div class="entity-cell">
          <strong><code>${escapeHtml(maskInviteCode(invite))}</code></strong>
          <span class="entity-meta">${escapeHtml(invite.raw_code ? "本次新生成" : "仅展示掩码")}</span>
        </div>
      </td>
      <td class="col-invite-status" data-label="状态">${inviteStatusBadge(invite.status)}</td>
      <td class="col-invite-usage" data-label="使用情况">
        <div class="metric-stack">
          <div class="metric-head">
            <strong class="mono">${fmtNumber(invite.used_count || 0)} / ${fmtNumber(invite.max_uses || 0)}</strong>
            <span class="table-note">剩余 ${fmtNumber(remaining)}</span>
          </div>
          ${renderCapacityBar(invite.used_count || 0, invite.max_uses || 0)}
        </div>
      </td>
      <td class="col-invite-benefit" data-label="权益">
        <div class="stack-cell">
          <span class="mono">额度 ${fmtNumber(invite.daily_quota || 0) || "0"}</span>
          <span class="table-note">并发 ${fmtNumber(invite.max_inflight || 0)}</span>
        </div>
      </td>
      <td class="col-invite-expiry" data-label="过期时间">
        <div class="stack-cell">
          <span class="mono">${escapeHtml(fmtDate(invite.expires_at))}</span>
          <span class="table-note">${escapeHtml(invite.expires_at ? fmtRelativeTime(invite.expires_at) : "永久")}</span>
        </div>
      </td>
      <td class="col-invite-note" data-label="备注">${invite.note ? escapeHtml(invite.note) : '<span class="table-note">—</span>'}</td>
      <td class="col-invite-actions" data-label="操作">
        <div class="table-actions">
          ${revokeButton}
          <button class="btn btn-danger" data-action="delete-invite" type="button">删除</button>
        </div>
      </td>
    </tr>
  `;
}

function bindInviteActions(items) {
  $$("#invitesTable tbody tr").forEach((row) => {
    const id = row.dataset.id;
    const invite = items.find((item) => item.id === id);
    if (!invite) return;

    const revokeBtn = row.querySelector('[data-action="revoke-invite"]');
    const deleteBtn = row.querySelector('[data-action="delete-invite"]');

    if (revokeBtn) {
      revokeBtn.addEventListener("click", async (event) => {
        if (!confirm(`确认撤销邀请码前缀 "${invite.code_prefix}" ?`)) return;
        try {
          await withBusyButton(event.currentTarget, "撤销中…", async () => {
            await api(`/api/admin/invite-codes/${id}/revoke`, { method: "POST" });
            await Promise.all([refreshInvites(), refreshOverview()]);
            renderInsightPanels();
          });
          showToast("邀请码已撤销", "success");
        } catch (err) {
          showToast(`撤销失败：${err.message}`, "error");
        }
      });
    }

    if (deleteBtn) {
      deleteBtn.addEventListener("click", async (event) => {
        if (!confirm(`确认删除邀请码前缀 "${invite.code_prefix}" ? 此操作不可恢复。`)) return;
        try {
          await withBusyButton(event.currentTarget, "删除中…", async () => {
            await api(`/api/admin/invite-codes/${id}`, { method: "DELETE" });
            await Promise.all([refreshInvites(), refreshOverview()]);
            renderInsightPanels();
          });
          showToast("邀请码已删除", "success");
        } catch (err) {
          showToast(
            isMissingDeleteRouteError(err)
              ? "删除失败：当前后端实例还没加载邀请码删除接口，请先重启 st-imagen 服务。"
              : `删除失败：${err.message}`,
            "error",
          );
        }
      });
    }
  });
}

function renderInvitesTable() {
  const tbody = $("#invitesTable tbody");
  const summary = $("#invitesSummary");
  const items = filteredInvites();
  const active = state.invites.filter((item) => item.status === "active").length;
  const remaining = state.invites.reduce(
    (sum, invite) => sum + Math.max(0, Number(invite.max_uses || 0) - Number(invite.used_count || 0)),
    0,
  );
  renderToolbarMeta(
    "invitesFilterMeta",
    "邀请码",
    items.length,
    state.invites.length,
    inviteFilterChips(),
    "invites",
    "支持搜索与筛选。",
  );
  if (summary) {
    summary.textContent = `共 ${fmtNumber(state.invites.length)} 个邀请码，${fmtNumber(active)} 个可用；剩余可用次数 ${fmtNumber(remaining)}。`;
  }

  if (!state.invites.length) {
    tbody.innerHTML = renderEmptyRow(7, "暂无邀请码。", "生成一批邀请码用于拉新或内测分发。");
    return;
  }
  if (!items.length) {
    tbody.innerHTML = renderEmptyRow(7, "没有匹配结果。", "尝试放宽筛选条件。");
    return;
  }
  tbody.innerHTML = items.map(renderInviteRow).join("");
  bindInviteActions(items);
}

async function refreshInvites() {
  const tbody = $("#invitesTable tbody");
  tbody.innerHTML = renderEmptyRow(7, "邀请码加载中…", "正在同步邀请码数据。");
  try {
    const data = await api("/api/admin/invite-codes");
    state.invites = Array.isArray(data.items) ? data.items : [];
    renderInvitesTable();
    renderInsightPanels();
    return true;
  } catch (err) {
    state.invites = [];
    renderToolbarMeta(
      "invitesFilterMeta",
      "邀请码",
      0,
      0,
      inviteFilterChips(),
      "invites",
      "支持搜索与筛选。",
    );
    tbody.innerHTML = renderErrorRow(7, err.message);
    const summary = $("#invitesSummary");
    if (summary) summary.textContent = `邀请码加载失败：${err.message}`;
    renderInsightPanels();
    return false;
  }
}

async function deleteAllInvites(button) {
  if (!state.invites.length) {
    showToast("当前没有邀请码可删除", "info");
    return;
  }
  if (!confirm("确认删除所有邀请码？已注册用户不会被删除，但邀请码记录不可恢复。")) return;
  try {
    await withBusyButton(button, "删除中…", async () => {
      try {
        await api("/api/admin/invite-codes", { method: "DELETE" });
      } catch (err) {
        if (err.status !== 404 && err.status !== 405) throw err;
        if (isMissingDeleteRouteError(err)) {
          throw new Error("当前后端实例还没加载邀请码批量删除接口，请先重启 st-imagen 服务。");
        }
        const failures = [];
        for (const invite of state.invites) {
          try {
            await api(`/api/admin/invite-codes/${invite.id}`, { method: "DELETE" });
          } catch (itemErr) {
            if (isMissingDeleteRouteError(itemErr)) {
              throw new Error("当前后端实例还没加载邀请码删除接口，请先重启 st-imagen 服务。");
            }
            failures.push(`${invite.code_prefix}: ${itemErr.message}`);
          }
        }
        if (failures.length) {
          const summary = failures.slice(0, 3).join("；");
          throw new Error(
            failures.length > 3 ? `${summary}；另外还有 ${failures.length - 3} 个失败` : summary,
          );
        }
      }
      await Promise.all([refreshInvites(), refreshOverview(), refreshUsers()]);
      renderInsightPanels();
    });
    showToast("全部邀请码已删除", "success");
  } catch (err) {
    showToast(`全部删除失败：${err.message}`, "error");
  }
}

function filteredLogs() {
  const query = state.filters.logs.query.trim().toLowerCase();
  return state.logs.filter((log) => {
    const matchesQuery =
      !query ||
      [log.prompt_preview, log.username, log.account_name, log.model, log.error_message]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(query));
    const matchesStatus = state.filters.logs.status === "all" || log.status === state.filters.logs.status;
    const matchesMode = state.filters.logs.mode === "all" || log.mode === state.filters.logs.mode;
    return matchesQuery && matchesStatus && matchesMode;
  });
}

function renderLogRow(log) {
  const statusBadge = log.status === "success"
    ? badgeHtml("成功", "success", "badge-compact")
    : badgeHtml("失败", "danger", "badge-compact");
  const errorMessage = String(log.error_message || "").trim();
  const errorSnippet = errorMessage ? truncateText(errorMessage, 68) : "";
  const images = parseOutputImages(log.output_images, log.output_preview);
  const specParts = [log.aspect_ratio, log.resolution].filter(Boolean);
  const preview =
    images.length || log.prompt_preview || errorMessage
      ? `<button class="btn btn-ghost" data-action="view-log" data-id="${escapeHtml(log.id)}" type="button">查看</button>`
      : '<span class="table-note">—</span>';
  const principal = [log.username, shortAccount(log.account_name)].filter(Boolean).join(" · ") || "—";
  return `
    <tr>
      <td class="col-log-time" data-label="时间">
        <div class="stack-cell">
          <span class="mono">${escapeHtml(fmtDate(log.timestamp))}</span>
          <span class="table-note">${escapeHtml(fmtRelativeTime(log.timestamp))}</span>
        </div>
      </td>
      <td class="col-log-request" data-label="请求">
        <div class="entity-cell">
          <strong>${escapeHtml(humanMode(log.mode))}</strong>
          ${log.is_stream ? `<span class="entity-meta">${badgeHtml("stream", "stream")}</span>` : ""}
        </div>
      </td>
      <td class="col-log-model" data-label="模型 / 规格">
        <div class="entity-cell">
          <strong>${escapeHtml(log.model || "未记录模型")}</strong>
          <span class="entity-meta">${escapeHtml(specParts.length ? specParts.join(" · ") : "默认规格")}</span>
        </div>
      </td>
      <td class="col-log-account" data-label="用户 · 账号">
        <div class="entity-cell">
          <strong>${escapeHtml(principal)}</strong>
          <span class="entity-meta">${escapeHtml(log.account_name || "未记录账号")}</span>
        </div>
      </td>
      <td class="col-log-duration" data-label="耗时">
        <div class="stack-cell">
          <span class="mono">${escapeHtml(fmtDuration(log.response_time_ms))}</span>
        </div>
      </td>
      <td class="col-log-status" data-label="状态">
        <div class="stack-cell log-status-cell">
          ${statusBadge}
          ${errorSnippet ? `<span class="table-note table-note-danger" title="${escapeHtml(errorMessage)}">${escapeHtml(errorSnippet)}</span>` : ""}
        </div>
      </td>
      <td class="col-log-preview" data-label="预览">${preview}</td>
    </tr>
  `;
}

function bindLogActions(items) {
  $$('#logsTable [data-action="view-log"]').forEach((button) => {
    button.addEventListener("click", () => {
      const log = items.find((item) => String(item.id) === String(button.dataset.id));
      if (log) openLogModal(log);
    });
  });
}

function renderLogsTable() {
  const tbody = $("#logsTable tbody");
  const summary = $("#logsSummary");
  const items = filteredLogs();
  const success = state.logs.filter((item) => item.status === "success").length;
  const error = state.logs.filter((item) => item.status === "error").length;
  renderToolbarMeta(
    "logsFilterMeta",
    "日志",
    items.length,
    state.logs.length,
    logFilterChips(),
    "logs",
    "支持搜索与筛选。",
  );
  if (summary) {
    summary.textContent = `当前缓存 ${fmtNumber(state.logs.length)} 条日志，成功 ${fmtNumber(success)}，失败 ${fmtNumber(error)}。`;
  }

  if (!state.logs.length) {
    tbody.innerHTML = renderEmptyRow(7, "暂无日志。", "生成完成后，这里会显示最近请求。");
    return;
  }
  if (!items.length) {
    tbody.innerHTML = renderEmptyRow(7, "没有匹配结果。", "尝试放宽筛选条件。");
    return;
  }
  tbody.innerHTML = items.map(renderLogRow).join("");
  bindLogActions(items);
}

async function refreshLogs() {
  const tbody = $("#logsTable tbody");
  tbody.innerHTML = renderEmptyRow(7, "日志加载中…", "正在同步最近生成记录。");
  try {
    const data = await api("/api/admin/logs?limit=80");
    state.logs = Array.isArray(data.items) ? data.items : [];
    renderLogsTable();
    renderInsightPanels();
    return true;
  } catch (err) {
    state.logs = [];
    renderToolbarMeta(
      "logsFilterMeta",
      "日志",
      0,
      0,
      logFilterChips(),
      "logs",
      "支持搜索与筛选。",
    );
    tbody.innerHTML = renderErrorRow(7, err.message);
    const summary = $("#logsSummary");
    if (summary) summary.textContent = `日志加载失败：${err.message}`;
    renderInsightPanels();
    return false;
  }
}

function setPreviewVisible(visible) {
  const modal = $("#previewModal");
  if (!modal) return;
  state.previewVisible = Boolean(visible);
  setModalVisible(modal, state.previewVisible);
  modal.setAttribute("aria-hidden", state.previewVisible ? "false" : "true");
}

function renderPreviewModal() {
  const prompt = state.previewPrompt || "暂无提示词。";
  const error = String(state.previewError || "").trim();
  const total = state.previewItems.length;
  const hasImages = total > 0;
  const activeIndex = hasImages ? clamp(state.previewIndex, 0, total - 1) : -1;
  const activeImage = hasImages ? state.previewItems[activeIndex] : "";
  const image = $("#previewImage");
  const emptyState = $("#previewEmptyState");
  const promptEl = $("#previewPrompt");
  const errorBlock = $("#previewErrorBlock");
  const errorEl = $("#previewErrorText");

  state.previewIndex = activeIndex;
  if (promptEl) promptEl.textContent = prompt;
  if (errorEl) errorEl.textContent = error;
  if (errorBlock) errorBlock.classList.toggle("is-hidden", !error);

  if (image) {
    if (activeImage) {
      image.onerror = () => {
        image.classList.add("is-hidden");
        if (emptyState) {
          emptyState.textContent = "图片已被清理或丢失";
          emptyState.classList.remove("is-hidden");
        }
      };
      image.onload = () => {
        if (emptyState) emptyState.classList.add("is-hidden");
      };
      image.src = activeImage;
      image.alt = prompt;
      image.classList.remove("is-hidden");
    } else {
      image.onerror = null;
      image.onload = null;
      image.removeAttribute("src");
      image.removeAttribute("alt");
      image.classList.add("is-hidden");
    }
  }

  if (emptyState) {
    emptyState.classList.toggle("is-hidden", hasImages);
    if (!hasImages) emptyState.textContent = "暂无输出结果";
  }
}

function openLogModal(log) {
  const images = parseOutputImages(log.output_images, log.output_preview);
  const prompt = String(log.prompt_preview || "").trim();
  const error = String(log.error_message || "").trim();
  if (!images.length && !prompt && !error) {
    showToast("暂无可展示内容。", "warning");
    return;
  }

  if (!state.previewVisible && document.activeElement instanceof HTMLElement) {
    state.previewReturnFocus = document.activeElement;
  }

  state.previewItems = images;
  state.previewIndex = images.length ? 0 : -1;
  state.previewPrompt = prompt;
  state.previewError = error;

  renderPreviewModal();
  setPreviewVisible(true);
  window.setTimeout(() => {
    $("#previewModalClose")?.focus({ preventScroll: true });
  }, 40);
}

function closeLogModal({ restoreFocus = true } = {}) {
  const wasOpen = state.previewVisible || state.previewItems.length > 0 || state.previewPrompt || state.previewError;
  setPreviewVisible(false);
  state.previewItems = [];
  state.previewIndex = -1;
  state.previewPrompt = "";
  state.previewError = "";

  const image = $("#previewImage");
  if (image) {
    image.removeAttribute("src");
    image.removeAttribute("alt");
    image.classList.remove("is-hidden");
  }
  const promptEl = $("#previewPrompt");
  if (promptEl) promptEl.textContent = "";
  const errorEl = $("#previewErrorText");
  if (errorEl) errorEl.textContent = "";
  $("#previewErrorBlock")?.classList.add("is-hidden");
  $("#previewEmptyState")?.classList.add("is-hidden");

  const returnFocusEl = state.previewReturnFocus;
  state.previewReturnFocus = null;
  if (
    wasOpen &&
    restoreFocus &&
    returnFocusEl &&
    typeof returnFocusEl.focus === "function" &&
    document.body.contains(returnFocusEl)
  ) {
    returnFocusEl.focus({ preventScroll: true });
  }
}

function stepLogPreview(delta) {
  if (!state.previewVisible || state.previewItems.length <= 1) return;
  const total = state.previewItems.length;
  state.previewIndex = (state.previewIndex + delta + total) % total;
  renderPreviewModal();
}

function bindPreviewModal() {
  const modal = $("#previewModal");
  const closeBtn = $("#previewModalClose");
  if (!modal || !closeBtn) return;

  closeBtn.addEventListener("click", () => closeLogModal());
  modal.addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeLogModal();
  });
}

async function refreshOverview() {
  try {
    state.overview = await api("/api/admin/stats/overview");
    renderInsightPanels();
    return true;
  } catch (err) {
    state.overview = null;
    renderInsightPanels();
    const metaEl = $("#overviewMeta");
    if (metaEl) metaEl.textContent = `总览接口加载失败：${err.message}`;
    return false;
  }
}

async function refreshAdminProfile() {
  try {
    state.admin = await api("/api/admin/me");
    renderSyncState();
    return true;
  } catch {
    state.admin = null;
    renderSyncState();
    return false;
  }
}

async function refreshAll() {
  if (state.refreshing) return;
  state.refreshing = true;
  state.sync = { status: "loading", successCount: 0, totalCount: 8 };
  const refreshButton = $("#refreshAllBtn");
  if (refreshButton) refreshButton.disabled = true;
  renderInsightPanels();

  const results = await Promise.all([
    refreshAdminProfile(),
    refreshOverview(),
    refreshAccounts(),
    refreshUsers(),
    refreshInvites(),
    refreshLogs(),
    refreshSettings(),
    refreshRuntimeStatus(),
  ]);

  state.refreshing = false;
  state.lastUpdatedAt = new Date();
  state.sync = {
    status: results.every(Boolean) ? "ready" : "partial",
    successCount: results.filter(Boolean).length,
    totalCount: results.length,
  };
  if (refreshButton) refreshButton.disabled = false;
  renderInsightPanels();
  if (state.sync.status === "partial") {
    showToast(`控制台已刷新，${state.sync.successCount}/${state.sync.totalCount} 个数据块成功。`, "warning");
  }
}

// ==================== 应用设置 ====================
const RETENTION_FIELDS = [
  {
    key: "generated_image_retention_days",
    inputId: "generatedRetentionInput",
    metaId: "generatedRetentionMeta",
    resetBtnId: "resetGeneratedRetentionBtn",
    label: "生成图片保存天数",
  },
  {
    key: "reference_upload_retention_days",
    inputId: "referenceRetentionInput",
    metaId: "referenceRetentionMeta",
    resetBtnId: "resetReferenceRetentionBtn",
    label: "参考图保存天数",
  },
];

function formatRetentionDays(value) {
  if (!Number.isFinite(value) || value <= 0) return "永久保留";
  return `${value} 天`;
}

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = "B";
  for (const next of units) {
    if (value < 1024) break;
    value /= 1024;
    unit = next;
  }
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${unit}`;
}

function formatCount(count) {
  return Number.isFinite(count) ? Number(count).toLocaleString("zh-CN") : "—";
}

let lastStorageStats = null;

function renderStorageStats(storage) {
  lastStorageStats = storage || null;
  const render = (id, stat, unitWord) => {
    const el = $(`#${id}`);
    if (!el) return;
    if (!stat) {
      el.textContent = "暂不可用";
      return;
    }
    el.textContent = `${formatCount(stat.count)} ${unitWord} · ${formatBytes(stat.size_bytes)}`;
  };
  render("storageLogsStat", storage?.logs, "条");
  render("storageGeneratedStat", storage?.generated_images, "张");
  render("storageReferenceStat", storage?.reference_images, "张");
}

// ==================== 存储清理 ====================
const CLEANUP_TARGET_META = {
  logs: { label: "生成日志", unit: "条", stat: () => lastStorageStats?.logs },
  generated_images: { label: "生成图片", unit: "张", stat: () => lastStorageStats?.generated_images },
  reference_images: { label: "参考图", unit: "张", stat: () => lastStorageStats?.reference_images },
};

async function runStorageCleanup(targets, button, actionLabel) {
  if (targets.every((key) => (CLEANUP_TARGET_META[key].stat()?.count ?? 0) === 0)) {
    showToast("当前没有可清理的内容", "info");
    return;
  }
  if (!confirm(`确认${actionLabel}？删除后不可恢复。`)) return;

  let data;
  try {
    data = await withBusyButton(button, "清理中…", async () =>
      api("/api/admin/settings/cleanup", {
        method: "POST",
        body: JSON.stringify({ targets }),
      })
    );
  } catch (err) {
    showToast(`清理失败：${err.message}`, "error");
    return;
  }

  renderStorageStats(data.storage);
  const parts = targets
    .map((key) => `${CLEANUP_TARGET_META[key].label} ${formatCount(data.removed?.[key])} ${CLEANUP_TARGET_META[key].unit}`)
    .join("，");
  showToast(`清理完成：${parts}`, "success");
}

function renderSettingsForm(items) {
  RETENTION_FIELDS.forEach((field) => {
    const item = items?.[field.key];
    const input = $(`#${field.inputId}`);
    const meta = $(`#${field.metaId}`);
    if (!input || !meta) return;
    if (!item) {
      meta.textContent = "暂不可用";
      return;
    }
    input.value = String(item.value);
    const source = item.overridden ? "自定义" : `默认 ${formatRetentionDays(item.default)}`;
    meta.textContent = `当前生效：${formatRetentionDays(item.value)}（${source}）`;
  });
}

async function refreshSettings() {
  try {
    const data = await api("/api/admin/settings");
    renderSettingsForm(data.items);
    renderStorageStats(data.storage);
    return true;
  } catch (err) {
    RETENTION_FIELDS.forEach((field) => {
      const meta = $(`#${field.metaId}`);
      if (meta) meta.textContent = `加载失败：${err.message}`;
    });
    return false;
  }
}

function parseRetentionInput(field) {
  const raw = $(`#${field.inputId}`)?.value?.trim();
  if (raw === "") {
    throw new Error(`${field.label}不能为空（0 表示永久保留）`);
  }
  const value = Number(raw);
  if (!Number.isFinite(value) || value < 0 || value > 3650) {
    throw new Error(`${field.label}需为 0 ~ 3650 之间的数字`);
  }
  return value;
}

async function saveSettings() {
  const body = {};
  try {
    RETENTION_FIELDS.forEach((field) => {
      body[field.key] = parseRetentionInput(field);
    });
  } catch (err) {
    showToast(err.message, "error");
    return;
  }

  const button = $("#saveSettingsBtn");
  if (button) button.disabled = true;
  try {
    const data = await api("/api/admin/settings", { method: "PUT", body: JSON.stringify(body) });
    renderSettingsForm(data.items);
    renderStorageStats(data.storage);
    showToast("设置已保存，并已按新策略执行一次清理", "success");
  } catch (err) {
    showToast(`保存失败：${err.message}`, "error");
  } finally {
    if (button) button.disabled = false;
  }
}

async function resetRetentionField(field) {
  const button = $(`#${field.resetBtnId}`);
  if (button) button.disabled = true;
  try {
    const data = await api("/api/admin/settings", {
      method: "PUT",
      body: JSON.stringify({ [field.key]: null }),
    });
    renderSettingsForm(data.items);
    renderStorageStats(data.storage);
    showToast(`${field.label}已恢复默认值`, "success");
  } catch (err) {
    showToast(`恢复默认失败：${err.message}`, "error");
  } finally {
    if (button) button.disabled = false;
  }
}

function bindSettingsPage() {
  const saveBtn = $("#saveSettingsBtn");
  if (saveBtn) saveBtn.addEventListener("click", saveSettings);
  const reloadBtn = $("#reloadSettingsBtn");
  if (reloadBtn) reloadBtn.addEventListener("click", refreshSettings);
  const reloadStorageBtn = $("#reloadStorageBtn");
  if (reloadStorageBtn) reloadStorageBtn.addEventListener("click", refreshSettings);
  $("#cleanupAllBtn")?.addEventListener("click", (event) =>
    runStorageCleanup(["logs", "generated_images", "reference_images"], event.currentTarget, "全部日志与图片")
  );
  $("#cleanupLogsBtn")?.addEventListener("click", (event) =>
    runStorageCleanup(["logs"], event.currentTarget, "全部生成日志")
  );
  $("#cleanupGeneratedBtn")?.addEventListener("click", (event) =>
    runStorageCleanup(["generated_images"], event.currentTarget, "全部生成图片")
  );
  $("#cleanupReferenceBtn")?.addEventListener("click", (event) =>
    runStorageCleanup(["reference_images"], event.currentTarget, "全部参考图")
  );
  RETENTION_FIELDS.forEach((field) => {
    const resetBtn = $(`#${field.resetBtnId}`);
    if (resetBtn) resetBtn.addEventListener("click", () => resetRetentionField(field));
  });
}

function openAccountModal(account = null) {  state.editing.accountId = account ? account.id : null;
  $("#accountModalTitle").textContent = account ? "编辑账号" : "新增账号";
  $("#m_name").value = account?.name || "";
  $("#m_org_id").value = account?.org_id || "";
  $("#m_flow_id").value = account?.flow_id || "";
  $("#m_api_key").value = "";
  $("#m_private_api_key").value = "";
  $("#m_status").value = account?.status || "active";
  $("#m_max_inflight").value = String(account?.max_inflight ?? 10);
  $("#m_api_key").placeholder = account ? "留空保持原值" : "sk-...";
  $("#m_private_api_key").placeholder = account?.private_api_key_set ? "留空保持原值" : "sk-...";
  $("#accountModalError").classList.add("is-hidden");
  setModalVisible("accountModal", true);
  window.setTimeout(() => $("#m_name").focus(), 40);
}

function closeAccountModal() {
  setModalVisible("accountModal", false);
}

function openAccountImportModal() {
  $("#accountImportModalError").classList.add("is-hidden");
  $("#accountImportResult").value = "";
  $("#accountImportFileName").textContent = "未选择文件";
  setModalVisible("accountImportModal", true);
}

function closeAccountImportModal() {
  setModalVisible("accountImportModal", false);
}

async function loadAccountImportFile(file) {
  const errEl = $("#accountImportModalError");
  errEl.classList.add("is-hidden");
  if (!file) return;
  try {
    const text = await file.text();
    $("#accountImportText").value = text;
    $("#accountImportFileName").textContent = file.name || "已载入文件";
  } catch (err) {
    errEl.textContent = `读取文件失败：${err.message}`;
    errEl.classList.remove("is-hidden");
  }
}

function formatAccountImportSummary(data) {
  const lines = [
    `输入 ${data.total_input || 0} 条`,
    `成功导入 ${data.created_count || 0} 条`,
    `重复跳过 ${data.skipped_count || 0} 条`,
    `无效/失败 ${data.invalid_count || 0} 条`,
  ];

  const created = Array.isArray(data.created) ? data.created : [];
  const skipped = Array.isArray(data.skipped) ? data.skipped : [];
  const invalid = Array.isArray(data.invalid) ? data.invalid : [];

  if (created.length) {
    lines.push("");
    lines.push("已导入：");
    created.slice(0, 20).forEach((item) => {
      lines.push(`- ${item.name} · ${item.org_id} · ${item.flow_id}`);
    });
    if (created.length > 20) lines.push(`- 其余 ${created.length - 20} 条省略`);
  }

  if (skipped.length) {
    lines.push("");
    lines.push("已跳过：");
    skipped.slice(0, 20).forEach((item) => {
      lines.push(`- 第 ${item.index} 条 ${item.email || ""}：${item.reason}`);
    });
    if (skipped.length > 20) lines.push(`- 其余 ${skipped.length - 20} 条省略`);
  }

  if (invalid.length) {
    lines.push("");
    lines.push("无效/失败：");
    invalid.slice(0, 20).forEach((item) => {
      lines.push(`- 第 ${item.index} 条 ${item.email || ""}：${item.reason}`);
    });
    if (invalid.length > 20) lines.push(`- 其余 ${invalid.length - 20} 条省略`);
  }

  return lines.join("\n");
}

async function saveAccountImport() {
  const errEl = $("#accountImportModalError");
  errEl.classList.add("is-hidden");
  const rawJson = $("#accountImportText").value.trim();
  if (!rawJson) {
    errEl.textContent = "请先选择 JSON 文件或粘贴 JSON 数据";
    errEl.classList.remove("is-hidden");
    return;
  }

  try {
    const data = await api("/api/admin/accounts/import", {
      method: "POST",
      body: JSON.stringify({ raw_json: rawJson }),
    });
    $("#accountImportResult").value = formatAccountImportSummary(data);
    await Promise.all([refreshAccounts(), refreshOverview()]);
    renderInsightPanels();
    showToast(`成功导入 ${data.created_count || 0} 个账号`, "success");
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove("is-hidden");
  }
}

async function saveAccount() {
  const errEl = $("#accountModalError");
  errEl.classList.add("is-hidden");

  const privateRaw = $("#m_private_api_key").value;
  const payload = {
    name: $("#m_name").value.trim(),
    org_id: $("#m_org_id").value.trim(),
    flow_id: $("#m_flow_id").value.trim(),
    api_key: $("#m_api_key").value.trim(),
    private_api_key: privateRaw.trim(),
    status: $("#m_status").value,
    max_inflight: Number($("#m_max_inflight").value || 10),
  };

  if (!Number.isFinite(payload.max_inflight) || payload.max_inflight < 1) {
    errEl.textContent = "最大并发必须是大于等于 1 的数字";
    errEl.classList.remove("is-hidden");
    return;
  }

  try {
    if (state.editing.accountId) {
      const body = Object.assign({}, payload);
      if (!body.api_key) delete body.api_key;
      if (privateRaw === "") delete body.private_api_key;
      await api(`/api/admin/accounts/${state.editing.accountId}`, {
        method: "PUT",
        body: JSON.stringify(body),
      });
      showToast("账号已更新", "success");
    } else {
      if (!payload.name || !payload.org_id || !payload.flow_id || !payload.api_key) {
        throw new Error("请填写名称、org_id、flow_id 和 api_key");
      }
      if (!payload.private_api_key) delete payload.private_api_key;
      await api("/api/admin/accounts", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      showToast("账号已创建", "success");
    }
    closeAccountModal();
    await Promise.all([refreshAccounts(), refreshOverview()]);
    renderInsightPanels();
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove("is-hidden");
  }
}

function setUserCreateMode(mode) {
  const nextMode = mode === "batch" ? "batch" : "manual";
  state.userCreateMode = nextMode;
  const isBatch = nextMode === "batch";
  $("#userCreateManualBtn")?.classList.toggle("btn-primary", !isBatch);
  $("#userCreateManualBtn")?.classList.toggle("btn-ghost", isBatch);
  $("#userCreateBatchBtn")?.classList.toggle("btn-primary", isBatch);
  $("#userCreateBatchBtn")?.classList.toggle("btn-ghost", !isBatch);
  $("#userUsernameField")?.classList.toggle("is-hidden", isBatch);
  $("#userPasswordField")?.classList.toggle("is-hidden", isBatch);
  $("#userBatchCountField")?.classList.toggle("is-hidden", !isBatch);
  $("#userBatchResultField")?.classList.toggle("is-hidden", !isBatch);
  $("#userBatchCopyBtn")?.classList.toggle("is-hidden", !isBatch);
  const saveBtn = $("#userModalSave");
  if (saveBtn && !state.editing.userId) {
    saveBtn.textContent = isBatch ? "批量创建" : "创建用户";
  }
}

function formatUserBatchResult(items) {
  return (items || [])
    .map((item) => `${item.username},${item.password}`)
    .join("\n");
}

function generateBatchUsername(existingUsernames = new Set()) {
  const bytes = new Uint8Array(4);
  for (let attempt = 0; attempt < 64; attempt += 1) {
    window.crypto.getRandomValues(bytes);
    const suffix = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    const username = `user-${suffix}`;
    if (!existingUsernames.has(username)) {
      existingUsernames.add(username);
      return username;
    }
  }
  throw new Error("自动生成用户名失败，请重试");
}

function generateBatchPassword(length = 12) {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789";
  const bytes = new Uint8Array(Math.max(8, length));
  window.crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => alphabet[byte % alphabet.length]).join("");
}

async function createUsersBatchFallback(basePayload, count) {
  const items = [];
  const existingUsernames = new Set(
    state.users.map((item) => String(item.username || "").trim().toLowerCase()).filter(Boolean),
  );

  for (let index = 0; index < count; index += 1) {
    let created = null;
    for (let attempt = 0; attempt < 16; attempt += 1) {
      const username = generateBatchUsername(existingUsernames);
      const password = generateBatchPassword();
      try {
        const user = await api("/api/admin/users", {
          method: "POST",
          body: JSON.stringify(Object.assign({}, basePayload, { username, password })),
        });
        created = {
          id: user?.id || "",
          username,
          password,
          status: user?.status || basePayload.status,
        };
        break;
      } catch (err) {
        if (err?.status === 400 && String(err.message || "").includes("用户名已存在")) {
          continue;
        }
        throw err;
      }
    }

    if (!created) {
      throw new Error(`第 ${index + 1} 个用户自动生成失败，请重试`);
    }
    items.push(created);
  }

  return { items, total: items.length, fallback: true };
}

async function copyUserBatchResults() {
  const text = $("#userBatchResultText")?.value.trim() || "";
  if (!text) {
    showToast("没有可复制的用户结果", "info");
    return;
  }
  try {
    await copyTextToClipboard(text);
    showToast("用户结果已复制", "success");
  } catch {
    showToast("复制失败，请手动复制文本框内容", "error");
  }
}

function openUserModal(user = null) {
  const isEdit = !!user;
  const usernameInput = $("#u_username");
  state.editing.userId = user ? user.id : null;
  $("#userModalTitle").textContent = isEdit ? `编辑用户 · ${user.username}` : "创建用户";
  $("#userModalSub").textContent = isEdit
    ? "用户设置"
    : "手动创建或批量创建";
  $("#userCreateModeRow").classList.toggle("is-hidden", isEdit);
  usernameInput.value = user?.username || "";
  usernameInput.readOnly = isEdit;
  usernameInput.placeholder = isEdit ? "" : "3-32 位小写字母、数字、点、下划线或中划线";
  $("#u_batch_count").value = "10";
  $("#u_status").value = user?.status || "active";
  $("#u_daily_quota").value = String(user?.daily_quota ?? 0);
  $("#u_max_inflight").value = String(user?.max_inflight ?? 2);
  $("#u_expires_at").value = isoToDatetimeLocal(user?.expires_at || "");
  $("#u_new_password").value = "";
  $("#u_new_password").placeholder = isEdit ? "至少 8 位；留空表示不修改" : "至少 8 位";
  $("#userPasswordLabel").textContent = isEdit ? "重置密码（可留空）" : "登录密码";
  $("#userPasswordHelp").textContent = isEdit ? "留空表示不修改。" : "至少 8 位，创建后用户可直接登录。";
  $("#userBatchResultText").value = "";
  $("#userModalError").classList.add("is-hidden");
  const saveBtn = $("#userModalSave");
  if (saveBtn) {
    saveBtn.textContent = isEdit ? "保存" : "创建用户";
  }
  setUserCreateMode(isEdit ? "manual" : "manual");
  setModalVisible("userModal", true);
  window.setTimeout(() => {
    if (isEdit) $("#u_daily_quota")?.focus();
    else if (state.userCreateMode === "batch") $("#u_batch_count")?.focus();
    else usernameInput?.focus();
  }, 40);
}

function closeUserModal() {
  setModalVisible("userModal", false);
}

async function saveUser() {
  const errEl = $("#userModalError");
  errEl.classList.add("is-hidden");
  const password = $("#u_new_password").value;
  const isEdit = !!state.editing.userId;
  let expiresAt = null;
  try {
    expiresAt = datetimeLocalToIso($("#u_expires_at").value);
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove("is-hidden");
    return;
  }

  const basePayload = {
    status: $("#u_status").value,
    daily_quota: Number($("#u_daily_quota").value || 0),
    max_inflight: Number($("#u_max_inflight").value || 1),
    expires_at: expiresAt,
  };

  try {
    if (isEdit) {
      const payload = Object.assign({}, basePayload, { new_password: password });
      if (!payload.new_password) delete payload.new_password;
      await api(`/api/admin/users/${state.editing.userId}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      showToast("用户已更新", "success");
      closeUserModal();
    } else {
      if (state.userCreateMode === "batch") {
        const count = Number($("#u_batch_count").value || 0);
        if (!Number.isInteger(count) || count < 1 || count > 200) {
          throw new Error("创建数量需在 1 到 200 之间");
        }
        let data = null;
        try {
          data = await api("/api/admin/users/batch", {
            method: "POST",
            body: JSON.stringify(Object.assign({}, basePayload, { count })),
          });
        } catch (err) {
          if (err?.status !== 404 && err?.status !== 405) {
            throw err;
          }
          data = await createUsersBatchFallback(basePayload, count);
          showToast("当前后端未加载批量接口，已自动切换兼容创建方式", "warning");
        }
        const items = Array.isArray(data.items) ? data.items : [];
        $("#userBatchResultText").value = formatUserBatchResult(items);
        showToast(`已创建 ${items.length} 个用户`, "success");
        window.setTimeout(() => {
          const result = $("#userBatchResultText");
          result?.focus();
          result?.select();
        }, 40);
      } else {
        const username = $("#u_username").value.trim();
        if (!username || !password) throw new Error("请填写用户名和密码");
        await api("/api/admin/users", {
          method: "POST",
          body: JSON.stringify(Object.assign({}, basePayload, { username, password })),
        });
        showToast("用户已创建", "success");
        closeUserModal();
      }
    }
    await Promise.all([refreshUsers(), refreshOverview()]);
    renderInsightPanels();
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove("is-hidden");
  }
}

function openInviteModal() {
  $("#inviteModalError").classList.add("is-hidden");
  $("#inviteResultText").value = "";
  setModalVisible("inviteModal", true);
}

function closeInviteModal() {
  setModalVisible("inviteModal", false);
}

async function saveInviteBatch() {
  const errEl = $("#inviteModalError");
  errEl.classList.add("is-hidden");

  const payload = {
    count: Number($("#i_count").value || 1),
    max_uses: Number($("#i_max_uses").value || 1),
    expires_in_days: Number($("#i_expires_in_days").value || 30),
    daily_quota: Number($("#i_daily_quota").value || 0),
    max_inflight: Number($("#i_max_inflight").value || 2),
    note: $("#i_note").value.trim(),
  };

  try {
    const data = await api("/api/admin/invite-codes", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const rawCodes = (data.items || []).map((item) => item.raw_code).filter(Boolean);
    $("#inviteResultText").value = rawCodes.join("\n");
    await Promise.all([refreshInvites(), refreshOverview()]);
    renderInsightPanels();
    showToast(`已生成 ${rawCodes.length} 个邀请码`, "success");
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove("is-hidden");
  }
}

async function copyInviteResults() {
  const text = $("#inviteResultText").value.trim();
  if (!text) {
    showToast("没有可复制的邀请码", "info");
    return;
  }
  try {
    await copyTextToClipboard(text);
    showToast("邀请码已复制", "success");
  } catch {
    showToast("复制失败，请手动复制文本框内容", "error");
  }
}

function bindFilters() {
  $("#accountSearchInput").addEventListener("input", (event) => {
    state.filters.accounts.query = event.currentTarget.value;
    renderAccountsTable();
  });
  $("#accountStatusFilter").addEventListener("change", (event) => {
    state.filters.accounts.status = event.currentTarget.value;
    renderAccountsTable();
  });
  $("#accountLoadFilter").addEventListener("change", (event) => {
    state.filters.accounts.load = event.currentTarget.value;
    renderAccountsTable();
  });

  $("#userSearchInput").addEventListener("input", (event) => {
    state.filters.users.query = event.currentTarget.value;
    renderUsersTable();
  });
  $("#userStatusFilter").addEventListener("change", (event) => {
    state.filters.users.status = event.currentTarget.value;
    renderUsersTable();
  });
  $("#userLifecycleFilter").addEventListener("change", (event) => {
    state.filters.users.lifecycle = event.currentTarget.value;
    renderUsersTable();
  });

  $("#inviteSearchInput").addEventListener("input", (event) => {
    state.filters.invites.query = event.currentTarget.value;
    renderInvitesTable();
  });
  $("#inviteStatusFilter").addEventListener("change", (event) => {
    state.filters.invites.status = event.currentTarget.value;
    renderInvitesTable();
  });

  $("#logSearchInput").addEventListener("input", (event) => {
    state.filters.logs.query = event.currentTarget.value;
    renderLogsTable();
  });
  $("#logStatusFilter").addEventListener("change", (event) => {
    state.filters.logs.status = event.currentTarget.value;
    renderLogsTable();
  });
  $("#logModeFilter").addEventListener("change", (event) => {
    state.filters.logs.mode = event.currentTarget.value;
    renderLogsTable();
  });
}

document.addEventListener("DOMContentLoaded", () => {
  if (window.STImagen) window.STImagen.bindThemeToggle();

  bindFilters();
  initConsoleNav();
  bindPreviewModal();
  bindSettingsPage();
  renderInsightPanels();

  if (getToken()) showDashboard();
  else showLogin();

  $("#loginBtn").addEventListener("click", login);
  $("#loginUsername").addEventListener("keydown", (event) => {
    if (event.key === "Enter") login();
  });
  $("#loginPassword").addEventListener("keydown", (event) => {
    if (event.key === "Enter") login();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.previewVisible) {
      closeLogModal();
      return;
    }
    if (state.previewVisible && event.key === "ArrowLeft") {
      event.preventDefault();
      stepLogPreview(-1);
      return;
    }
    if (state.previewVisible && event.key === "ArrowRight") {
      event.preventDefault();
      stepLogPreview(1);
    }
  });
  $("#logoutLink").addEventListener("click", (event) => {
    event.preventDefault();
    logout();
  });

  $("#refreshAllBtn").addEventListener("click", refreshAll);

  $("#importAccountsBtn").addEventListener("click", openAccountImportModal);
  $("#newAccountBtn").addEventListener("click", () => openAccountModal());
  $("#reloadAccountsBtn").addEventListener("click", async () => {
    await Promise.all([refreshAccounts(), refreshOverview()]);
    renderInsightPanels();
  });
  $("#enableAllAccountsBtn").addEventListener("click", (event) => bulkUpdateAccountStatus(event.currentTarget, "active"));
  $("#disableAllAccountsBtn").addEventListener("click", (event) => bulkUpdateAccountStatus(event.currentTarget, "disabled"));
  $("#deleteAllAccountsBtn").addEventListener("click", (event) => deleteAllAccounts(event.currentTarget));

  $("#reloadUsersBtn").addEventListener("click", async () => {
    await Promise.all([refreshUsers(), refreshOverview()]);
    renderInsightPanels();
  });
  $("#newUserBtn").addEventListener("click", () => openUserModal());
  $("#userCreateManualBtn").addEventListener("click", () => {
    setUserCreateMode("manual");
    $("#u_username")?.focus();
  });
  $("#userCreateBatchBtn").addEventListener("click", () => {
    setUserCreateMode("batch");
    $("#u_batch_count")?.focus();
  });
  $("#userBatchCopyBtn").addEventListener("click", copyUserBatchResults);
  $("#deleteAllUsersBtn").addEventListener("click", (event) => deleteAllUsers(event.currentTarget));

  $("#reloadInvitesBtn").addEventListener("click", async () => {
    await Promise.all([refreshInvites(), refreshOverview()]);
    renderInsightPanels();
  });
  $("#deleteAllInvitesBtn").addEventListener("click", (event) => deleteAllInvites(event.currentTarget));
  $("#newInviteBtn").addEventListener("click", openInviteModal);

  $("#reloadLogsBtn").addEventListener("click", async () => {
    await refreshLogs();
    renderInsightPanels();
  });

  $("#accountModalCancel").addEventListener("click", closeAccountModal);
  $("#accountModalSave").addEventListener("click", saveAccount);
  $("#accountModal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeAccountModal();
  });

  $("#accountImportPickFile").addEventListener("click", () => $("#accountImportFile").click());
  $("#accountImportFile").addEventListener("change", async (event) => {
    const [file] = Array.from(event.target.files || []);
    await loadAccountImportFile(file);
    event.target.value = "";
  });
  $("#accountImportModalCancel").addEventListener("click", closeAccountImportModal);
  $("#accountImportModalSave").addEventListener("click", saveAccountImport);
  $("#accountImportModal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeAccountImportModal();
  });

  $("#userModalCancel").addEventListener("click", closeUserModal);
  $("#userModalSave").addEventListener("click", saveUser);
  $("#userModal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeUserModal();
  });

  $("#inviteModalCancel").addEventListener("click", closeInviteModal);
  $("#inviteModalSave").addEventListener("click", saveInviteBatch);
  $("#inviteCopyBtn").addEventListener("click", copyInviteResults);
  $("#inviteModal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeInviteModal();
  });
});
