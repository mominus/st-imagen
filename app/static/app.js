// 前端：文生图 / 图生图（调用本服务的 /api/generate/stream）
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const ADMIN_TOKEN_KEY = "image_gen_admin_token";
const UNAUTHENTICATED_HINT = "请先登录";
const AUTH_FIELD_LABELS = {
  invite_code: "邀请码",
  username: "用户名",
  password: "密码",
};

const state = {
  mode: "text2img",
  options: null, // 缓存 /api/options 返回的原始结构
  abortCtrl: null,
  isUploadingReference: false,
  isGenerating: false,
  referenceImages: [],
  authenticated: false,
  user: null,
  admin: null,
  adminToken: null,
  authKind: "none",
  authMode: "login",
  authGateVisible: false,
  linuxdoEnabled: false,
  galleryItems: [],
  previewVisible: false,
  previewIndex: -1,
  previewReturnFocus: null,
  previewCopyResetTimer: null,
  errorHideTimer: null,
};

const MAX_REFERENCE_IMAGES = 5;
const REFERENCE_UPLOAD_MAX_BYTES = 20 * 1024 * 1024;
const REFERENCE_UPLOAD_DEFAULT_TEXT = "";
const RECENT_IMAGES_LIMIT = 24;
// 服务端工作流允许 200s 无进度；浏览器再留 20s 保护余量。
const GENERATE_STREAM_IDLE_TIMEOUT_MS = 220 * 1000;
const IMG2IMG_PREVIEW_DEFAULT_ASPECT_RATIO = "";
const IMG2IMG_PREVIEW_DEFAULT_RESOLUTION = "1K";
const GPT_IMAGE_2_MODEL = "GPT Image 2";
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
  } catch (_) {
    return null;
  }
}

function getShanghaiDateParts(value) {
  if (window.STImagen?.getShanghaiDateParts) {
    return window.STImagen.getShanghaiDateParts(value);
  }
  try {
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
  } catch (_) {
    return null;
  }
}

function setLoading(loading) {
  const btn = $("#generateBtn");
  const label = $("#genLabel");
  btn.disabled = loading || !state.authenticated;
  label.innerHTML = loading
    ? '<span class="spinner"></span> 生成中…'
    : "开始生成";
}

function setProgress({ visible, label, fill }) {
  const box = $("#progressBox");
  if (!visible) {
    box.classList.add("is-hidden");
    return;
  }
  box.classList.remove("is-hidden");
  if (label !== undefined) $("#progressLabel").textContent = label;
  if (fill !== undefined) $("#progressFill").style.width = `${Math.min(98, Math.max(5, fill))}%`;
}

function syncModalBodyState() {
  document.body.classList.toggle("modal-open", state.authGateVisible || state.previewVisible);
}

function showError(msg, { autoHide = true } = {}) {
  const el = $("#errorBox");
  if (state.errorHideTimer) {
    window.clearTimeout(state.errorHideTimer);
    state.errorHideTimer = null;
  }
  if (!msg) {
    el.classList.add("is-hidden");
    el.textContent = "";
    return;
  }
  el.textContent = msg;
  el.classList.remove("is-hidden");
  if (!autoHide) return;
  state.errorHideTimer = window.setTimeout(() => {
    // 只有当前仍是同一条提示时才自动隐藏，避免覆盖后来出现的新提示。
    if (el.textContent === msg) {
      el.classList.add("is-hidden");
      el.textContent = "";
    }
    state.errorHideTimer = null;
  }, 3000);
}

async function readSseChunkWithIdleTimeout(reader, abortCtrl, timeoutMs) {
  let timerId = null;
  try {
    return await Promise.race([
      reader.read(),
      new Promise((_, reject) => {
        timerId = window.setTimeout(() => {
          try {
            abortCtrl?.abort();
          } catch (_) {}
          const err = new Error(
            `长时间未收到生成进度更新（>${Math.round(timeoutMs / 1000)}s），请重试`
          );
          err.name = "StreamIdleTimeoutError";
          reject(err);
        }, timeoutMs);
      }),
    ]);
  } finally {
    if (timerId) window.clearTimeout(timerId);
  }
}

function showAuthError(msg) {
  const el = $("#authErrorBox");
  if (!el) return;
  if (!msg) {
    el.classList.add("is-hidden");
    el.textContent = "";
    return;
  }
  el.textContent = msg;
  el.classList.remove("is-hidden");
}

function switchAuthMode(mode) {
  state.authMode = mode === "activate" ? "activate" : "login";
  const isLogin = state.authMode === "login";
  $$("#authTabs .auth-tab").forEach((btn) => {
    const active = btn.dataset.authMode === state.authMode;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  $("#authLoginPane").classList.toggle("is-hidden", !isLogin);
  $("#authActivatePane").classList.toggle("is-hidden", isLogin);
  showAuthError("");
}

function setAuthGateVisible(visible, { animate = false } = {}) {
  const modal = $("#authModal");
  if (!modal) return;
  state.authGateVisible = Boolean(visible);
  modal.classList.toggle("show", state.authGateVisible);
  modal.setAttribute("aria-hidden", state.authGateVisible ? "false" : "true");
  syncModalBodyState();
  if (!state.authGateVisible) {
    showAuthError("");
  }
}

function openAuthGate({ mode = "login", message = "", focus = true, animate = true } = {}) {
  if (state.authenticated) return;
  switchAuthMode(mode);
  setAuthGateVisible(true, { animate });
  if (message) {
    showAuthError(message);
  }
  if (!focus) return;
  window.setTimeout(() => {
    const input = state.authMode === "login" ? $("#authLoginUsername") : $("#authInviteCode");
    input?.focus({ preventScroll: true });
  }, 160);
}

function closeAuthGate() {
  if (state.authenticated) return;
  setAuthGateVisible(false);
}

function syncLinuxdoButtons() {
  const visible = Boolean(state.linuxdoEnabled) && !state.authenticated;
  $$("#authModal .auth-oauth-btn").forEach((btn) => {
    btn.classList.toggle("is-hidden", !visible);
  });
  $("#authActivateActions")?.classList.toggle("is-hidden", Boolean(state.linuxdoEnabled));
  $("#authActivateOauth")?.classList.toggle("is-hidden", !visible);
}

async function startLinuxdoLogin(inviteCode) {
  showAuthError("");
  try {
    const r = await fetch("/api/auth/linuxdo/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ invite_code: inviteCode || "" }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || !data.authorize_url) {
      showAuthError(extractApiMessage(data, "LINUX DO 登录暂不可用"));
      return;
    }
    window.location.href = data.authorize_url;
  } catch (_) {
    showAuthError("LINUX DO 登录请求失败，请稍后重试");
  }
}

// OAuth 回调失败会 303 回 /?auth_error=...；展示后清理地址栏，避免刷新重复提示
function consumeAuthErrorParam() {
  const params = new URLSearchParams(window.location.search);
  const message = params.get("auth_error");
  if (!message) return;
  params.delete("auth_error");
  const query = params.toString();
  window.history.replaceState(
    {},
    "",
    `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`
  );
  openAuthGate({
    mode: message.indexOf("邀请码") !== -1 ? "activate" : "login",
    message,
    focus: false,
    animate: false,
  });
}

function formatValidationMessage(item) {
  if (!item || typeof item !== "object") return "";
  const loc = Array.isArray(item.loc)
    ? item.loc
        .map((part) => String(part || "").trim())
        .filter((part) => part && part !== "body" && part !== "query" && part !== "path")
    : [];
  const fieldKey = loc.length ? loc[loc.length - 1] : "";
  const fieldLabel = AUTH_FIELD_LABELS[fieldKey] || fieldKey;
  const type = String(item.type || "");
  const ctx = item.ctx && typeof item.ctx === "object" ? item.ctx : {};

  if (type === "missing") {
    return fieldLabel ? `请填写${fieldLabel}` : "请求参数缺失";
  }
  if (type === "string_too_short") {
    const minLength = Number(ctx.min_length || 0);
    return fieldLabel && minLength ? `${fieldLabel}至少 ${minLength} 位` : String(item.msg || "").trim();
  }
  if (type === "string_too_long") {
    const maxLength = Number(ctx.max_length || 0);
    return fieldLabel && maxLength ? `${fieldLabel}最多 ${maxLength} 位` : String(item.msg || "").trim();
  }
  if (type === "string_pattern_mismatch" && fieldLabel) {
    return `${fieldLabel}格式不正确`;
  }

  const message =
    typeof item.msg === "string"
      ? item.msg.trim()
      : typeof item.message === "string"
        ? item.message.trim()
        : "";
  if (!message) return "";
  return fieldLabel ? `${fieldLabel}：${message}` : message;
}

function extractApiMessage(payload, fallback) {
  if (payload && typeof payload.message === "string" && payload.message.trim()) {
    return payload.message.trim();
  }

  const detail = payload?.detail;
  if (typeof detail === "string" && detail.trim()) {
    return detail.trim();
  }
  if (Array.isArray(detail)) {
    const messages = detail.map(formatValidationMessage).filter(Boolean);
    if (messages.length) return messages.join("；");
  }
  if (detail && typeof detail === "object" && typeof detail.message === "string" && detail.message.trim()) {
    return detail.message.trim();
  }
  return fallback;
}

function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatQuotaText(user) {
  if (!user) return "";
  const quota = Math.max(0, Number(user.daily_quota || 0));
  const used = Math.max(0, Number(user.daily_used || 0));
  if (!quota) return "剩余 不限";
  return `剩余 ${Math.max(0, quota - used)}`;
}

function formatQuotaTitle(user) {
  if (!user) return "";
  const quota = Math.max(0, Number(user.daily_quota || 0));
  const used = Math.max(0, Number(user.daily_used || 0));
  if (user.quota_type === "one_time") {
    // 临时用户额度为一次性，悬浮不展示额度总量，只提示已用
    return quota ? `已用${used}` : `已用${used},额度不限`;
  }
  if (!quota) {
    return `今日已用${used},每日额度不限`;
  }
  return `今日已用${used},每日额度${quota}`;
}

function getStoredAdminToken() {
  try {
    return localStorage.getItem(ADMIN_TOKEN_KEY) || "";
  } catch (_) {
    return "";
  }
}

function setStoredAdminToken(token) {
  try {
    if (!token) localStorage.removeItem(ADMIN_TOKEN_KEY);
    else localStorage.setItem(ADMIN_TOKEN_KEY, token);
  } catch (_) {}
}

function getActiveAuthHeaders(headers = {}) {
  if (state.authKind === "admin" && state.adminToken) {
    return Object.assign({}, headers, {
      Authorization: `Bearer ${state.adminToken}`,
    });
  }
  return Object.assign({}, headers);
}

async function loadAdminProfile(adminToken) {
  try {
    const r = await fetch("/api/admin/me", {
      headers: {
        Authorization: `Bearer ${adminToken}`,
      },
    });
    if (r.status === 401) {
      setStoredAdminToken("");
      return null;
    }
    if (!r.ok) return null;
    return await r.json();
  } catch (_) {
    return null;
  }
}

async function refreshUserProfile() {
  if (state.authKind !== "user") return null;
  try {
    const r = await fetch("/api/auth/me");
    if (r.status === 401) {
      await handleUnauthorized("登录已失效，请重新登录");
      return null;
    }
    if (!r.ok) return null;
    const user = await r.json().catch(() => null);
    if (!user) return null;
    applyAuthState({ kind: "user", user });
    return user;
  } catch (_) {
    return null;
  }
}

function scheduleUserQuotaRefresh() {
  window.setTimeout(() => {
    refreshUserProfile();
  }, 250);
}

function applyAuthState(auth) {
  const authKind = auth?.kind || "none";
  const user = auth?.user || null;
  const admin = auth?.admin || null;

  state.authenticated = authKind !== "none";
  state.authKind = authKind;
  state.user = user;
  state.admin = admin;
  state.adminToken = authKind === "admin" ? (auth?.adminToken || getStoredAdminToken()) : null;

  const authEntryBtn = $("#authEntryBtn");
  const badge = $("#userBadge");
  const quotaBadge = $("#quotaBadge");
  const logoutBtn = $("#userLogoutBtn");
  const hint = $("#genHint");
  const badgeText = authKind === "admin"
    ? `管理员 @${admin?.username || "admin"}`
    : user?.display_name || (user?.username ? `@${user.username}` : "");
  const quotaText = authKind === "user" ? formatQuotaText(user) : "";
  const hintText = state.authenticated ? "" : "点击右上角登录后，才能调用生图服务。";

  if (state.authenticated) {
    setAuthGateVisible(false);
  } else {
    setAuthGateVisible(state.authGateVisible);
  }
  authEntryBtn.classList.toggle("is-hidden", state.authenticated);
  badge.classList.toggle("is-hidden", !badgeText);
  quotaBadge.classList.toggle("is-hidden", !quotaText);
  logoutBtn.classList.toggle("is-hidden", !state.authenticated);
  badge.textContent = badgeText;
  badge.title = badgeText;
  quotaBadge.textContent = quotaText;
  quotaBadge.title = quotaText ? formatQuotaTitle(user) : "";
  if (hint) {
    hint.textContent = hintText;
    hint.classList.toggle("is-hidden", !hintText);
  }
  if (!state.authenticated) {
    switchAuthMode(state.authMode || "login");
  }
  syncLinuxdoButtons();
  setLoading(state.isGenerating);
  if (state.authenticated) {
    void loadRecentImages();
  } else {
    renderResults([], "", {
      title: "登录后可查看最近生成的图片。",
      detail: "生成记录会保存在服务器，并按最新时间排在最前面。",
    });
  }
}

async function loadAuthStatus() {
  try {
    const r = await fetch("/api/auth/status");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    state.linuxdoEnabled = Boolean(data.linuxdo_enabled);
    if (data.user) {
      applyAuthState({ kind: "user", user: data.user });
      return;
    }
  } catch (_) {}

  const adminToken = getStoredAdminToken();
  if (adminToken) {
    const admin = await loadAdminProfile(adminToken);
    if (admin) {
      applyAuthState({ kind: "admin", admin, adminToken });
      return;
    }
  }
  applyAuthState(null);
}

async function handleUnauthorized(message) {
  const previousAuthKind = state.authKind;
  if (state.authKind === "admin") {
    setStoredAdminToken("");
  }
  applyAuthState(null);
  showError(message || "登录已失效，请重新登录");
  if (previousAuthKind !== "admin") {
    openAuthGate({ mode: "login", message: message || "登录已失效，请重新登录", focus: true });
  }
}


async function submitLogin() {
  showAuthError("");
  const username = $("#authLoginUsername").value.trim();
  const password = $("#authLoginPassword").value;
  if (!username || !password) {
    showAuthError("请输入用户名和密码");
    return;
  }

  try {
    const r = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || !data.success) {
      showAuthError(extractApiMessage(data, "用户名或密码错误"));
      return;
    }

    $("#authLoginPassword").value = "";
    applyAuthState({ kind: "user", user: data.user || null });
    showError("");
  } catch (_) {
    showAuthError("账号登录请求失败，请稍后重试");
  }
}

async function submitActivation() {
  if (state.linuxdoEnabled) {
    showAuthError("请使用 LINUX DO 登录完成注册");
    return;
  }
  showAuthError("");
  const inviteCode = $("#authInviteCode").value.trim();
  if (!inviteCode) {
    showAuthError("请输入邀请码");
    return;
  }

  try {
    const r = await fetch("/api/auth/invite-login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ invite_code: inviteCode }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || !data.success) {
      showAuthError(extractApiMessage(data, "邀请码进入失败"));
      return;
    }

    $("#authInviteCode").value = "";
    applyAuthState({ kind: "user", user: data.user || null });
    showError("");
  } catch (_) {
    showAuthError("邀请码登录请求失败，请稍后重试");
  }
}

function submitInviteRegistration() {
  const inviteCode = $("#authInviteCode").value.trim();
  if (!inviteCode) {
    showAuthError("请输入邀请码");
    return;
  }
  if (state.linuxdoEnabled) {
    void startLinuxdoLogin(inviteCode);
    return;
  }
  void submitActivation();
}

async function logoutUser() {
  try {
    await fetch("/api/auth/logout", { method: "POST" });
  } catch (_) {}
  if (state.authKind === "admin") {
    setStoredAdminToken("");
  }
  applyAuthState(null);
}

function setReferenceUploadState({ text, error = false } = {}) {
  const row = $("#img2imgUploadRow");
  const status = $("#referenceUploadStatus");
  if (!row || !status) return;

  row.classList.toggle("is-uploading", state.isUploadingReference);
  row.classList.toggle("has-file", state.referenceImages.length > 0);
  row.classList.toggle("has-error", error);
  status.textContent = text || REFERENCE_UPLOAD_DEFAULT_TEXT;
  row.setAttribute("aria-disabled", state.isUploadingReference ? "true" : "false");
  row.setAttribute("aria-busy", state.isUploadingReference ? "true" : "false");
}

function getManualReferenceUrl() {
  const input = $("#imageUrl");
  return input ? input.value.trim() : "";
}

function getTotalReferenceCount() {
  return state.referenceImages.length;
}

function syncReferenceUploadUi({ errorText = "" } = {}) {
  let text = "";
  if (state.isUploadingReference) {
    text = "上传中…";
  } else if (errorText) {
    text = errorText;
  } else if (state.referenceImages.length) {
    text = `${state.referenceImages.length}/${MAX_REFERENCE_IMAGES}张`;
  }
  setReferenceUploadState({ text, error: Boolean(errorText) });
}

function formatRecentTimestamp(value) {
  if (!value) return "";
  try {
    const parts = getShanghaiDateParts(value);
    if (!parts) return "";
    return `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
  } catch (_) {
    return "";
  }
}

function normalizeResultEntries(items) {
  return (items || [])
    .map((item) => {
      if (typeof item === "string") {
        return {
          id: "",
          generationId: "",
          url: item,
          timestamp: "",
          promptPreview: "",
          mode: "",
          model: "",
          aspectRatio: "",
          resolution: "",
          responseTimeMs: null,
        };
      }
      if (!item || typeof item !== "object") {
        return null;
      }
      const url = String(item.image_url || item.url || "").trim();
      if (!url) return null;
      const model = String(item.model || "").trim();
      const isGptImage2 = model === GPT_IMAGE_2_MODEL;
      return {
        id: String(item.id || "").trim(),
        generationId: String(item.generation_id || item.generationId || "").trim(),
        url,
        timestamp: String(item.timestamp || "").trim(),
        promptPreview: String(item.prompt || item.prompt_preview || item.promptPreview || "").trim(),
        mode: String(item.mode || "").trim(),
        model,
        aspectRatio: String(
          isGptImage2
            ? item.size || item.aspect_ratio || item.aspectRatio || ""
            : item.aspect_ratio || item.aspectRatio || item.size || "",
        ).trim(),
        resolution: String(
          isGptImage2 ? item.quality || item.resolution || "" : item.resolution || item.quality || "",
        ).trim(),
        size: String(item.size || "").trim(),
        quality: String(item.quality || "").trim(),
        responseTimeMs: Number(item.response_time_ms || item.responseTimeMs || 0) || null,
      };
    })
    .filter(Boolean);
}

function formatModeLabel(mode) {
  if (mode === "img2img") return "图生图";
  if (mode === "text2img") return "文生图";
  return "生成结果";
}

function formatResolutionSummary(entry) {
  const parts = [];
  if (entry?.model === GPT_IMAGE_2_MODEL) {
    if (entry?.aspectRatio) parts.push(entry.aspectRatio);
    if (entry?.resolution) parts.push(entry.resolution);
  } else {
    if (entry?.resolution) parts.push(entry.resolution);
    if (entry?.aspectRatio) parts.push(entry.aspectRatio);
  }
  return parts.join(" · ");
}

function getPromptDisplayText(entry, fallback = "未记录提示词") {
  const text = String(entry?.promptPreview || "").trim();
  return text || fallback;
}

function renderResultsEmpty(primary, secondary) {
  const wrap = $("#results");
  const empty = $("#emptyHint");
  wrap.innerHTML = "";
  empty.classList.remove("is-hidden");
  empty.innerHTML = `
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <circle cx="9" cy="9" r="1.5" />
      <path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21" />
    </svg>
    <p>${escapeHtml(primary)}</p>
    <span>${escapeHtml(secondary)}</span>
  `;
  $("#resultMeta").textContent = "";
}

function renderReferenceThumbs() {
  const wrap = $("#referenceThumbs");
  if (!wrap) return;

  if (state.mode !== "img2img" || !state.referenceImages.length) {
    wrap.classList.add("is-hidden");
    wrap.innerHTML = "";
    return;
  }

  wrap.classList.remove("is-hidden");
  const frag = document.createDocumentFragment();

  state.referenceImages.forEach((item, idx) => {
    const thumb = document.createElement("div");
    thumb.className = "reference-thumb";
    thumb.innerHTML = `
      <img src="${item.url}" alt="reference-${idx + 1}" loading="lazy" />
      <button type="button" class="reference-thumb-remove" data-index="${idx}" aria-label="移除参考图 ${idx + 1}">×</button>
    `;
    frag.appendChild(thumb);
  });

  wrap.innerHTML = "";
  wrap.appendChild(frag);
}

// items 可以是字符串数组，或 [{label, value}] 数组。
function fillSelect(selectEl, items, defaultValue) {
  selectEl.innerHTML = "";
  const normalized = (items || []).map((it) =>
    typeof it === "string" ? { label: it, value: it } : it,
  );
  normalized.forEach((it) => {
    const opt = document.createElement("option");
    opt.value = it.value;
    opt.textContent = it.label;
    selectEl.appendChild(opt);
  });
  const values = normalized.map((it) => it.value);
  if (defaultValue && values.includes(defaultValue)) {
    selectEl.value = defaultValue;
  } else if (values.length) {
    selectEl.value = values[0];
  }
}

function isGptImage2Selected() {
  return state.mode === "text2img" && $("#model").value === GPT_IMAGE_2_MODEL;
}

function applyText2imgDimensionOptions() {
  const isGptImage2 = isGptImage2Selected();
  const options = state.options.text2img || {};
  const aspectRatioLabel = $("#aspectRatioLabel");
  const resolutionLabel = $("#resolutionLabel");

  aspectRatioLabel.textContent = isGptImage2 ? "Size" : "画幅";
  resolutionLabel.textContent = isGptImage2 ? "Quality" : "清晰度";
  $("#aspectRatio").setAttribute("aria-label", isGptImage2 ? "Size" : "宽高比");
  $("#resolution").setAttribute("aria-label", isGptImage2 ? "Quality" : "分辨率");

  fillSelect(
    $("#aspectRatio"),
    isGptImage2 ? options.sizes || [] : options.aspect_ratios || [],
    isGptImage2 ? "auto" : null,
  );
  fillSelect(
    $("#resolution"),
    isGptImage2 ? options.qualities || [] : options.resolutions || [],
    isGptImage2 ? "auto" : null,
  );
}

function applyModeOptions() {
  if (!state.options) return;
  const modeOpt = state.options[state.mode] || {};
  fillSelect($("#model"), modeOpt.models || [], null);
  if (state.mode === "text2img") {
    applyText2imgDimensionOptions();
  }
}

async function loadOptions() {
  try {
    const r = await fetch("/api/options");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    state.options = await r.json();
    applyModeOptions();
  } catch (err) {
    showError(`加载选项失败：${err.message}`);
  }
}

function bindModeTabs() {
  $$("#modeTabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$("#modeTabs button").forEach((b) => {
        b.classList.remove("active");
        b.setAttribute("aria-selected", "false");
      });
      btn.classList.add("active");
      btn.setAttribute("aria-selected", "true");
      state.mode = btn.dataset.mode;
      const isImg2Img = state.mode === "img2img";
      // 用 class 而非 inline style 切换，配合 .subrow { display: contents }
      $("#img2imgUploadRow").classList.toggle("is-hidden", !isImg2Img);
      $("#img2imgRow").classList.toggle("is-hidden", !isImg2Img);
      $("#text2imgExtras").classList.toggle("is-hidden", isImg2Img);
      // 两个模式的可选模型不同，重新填充
      applyModeOptions();
      renderReferenceThumbs();
      syncReferenceUploadUi();
    });
  });
}

async function uploadReferenceFile(file) {
  if (!state.authenticated) {
    openAuthGate({ mode: "login", message: UNAUTHENTICATED_HINT, focus: true });
    throw new Error(UNAUTHENTICATED_HINT);
  }
  if (!file.type.startsWith("image/")) {
    throw new Error("参考图上传只支持图片文件");
  }
  if (file.size > REFERENCE_UPLOAD_MAX_BYTES) {
    const limitMb = REFERENCE_UPLOAD_MAX_BYTES / (1024 * 1024);
    throw new Error(`参考图不能超过 ${limitMb} MB`);
  }

  const formData = new FormData();
  formData.append("file", file);
  const r = await fetch("/api/reference-image", {
    method: "POST",
    headers: getActiveAuthHeaders(),
    body: formData,
  });

  let payload = null;
  try {
    payload = await r.json();
  } catch (_) {
    payload = null;
  }
  if (!r.ok) {
    if (r.status === 401) {
      await handleUnauthorized("登录已失效，请重新登录");
      throw new Error("登录已失效，请重新登录");
    }
    const detail = payload?.detail;
    throw new Error(detail?.message || detail || `HTTP ${r.status}`);
  }

  return {
    url: payload.url,
    name: payload.filename || file.name,
    sizeBytes: payload.size_bytes || file.size,
  };
}

async function uploadReferenceFiles(files) {
  const picked = Array.from(files || []);
  if (!picked.length) return;

  showError("");
  const remain = MAX_REFERENCE_IMAGES - getTotalReferenceCount();
  if (remain <= 0) {
    const msg = `参考图最多 ${MAX_REFERENCE_IMAGES} 张（含 URL）`;
    syncReferenceUploadUi({ errorText: msg });
    showError(msg);
    return;
  }

  const selected = picked.slice(0, remain);
  if (picked.length > remain) {
    showError(`最多保留 ${MAX_REFERENCE_IMAGES} 张参考图（含 URL），其余已忽略`);
  }

  state.isUploadingReference = true;
  syncReferenceUploadUi();

  try {
    for (const file of selected) {
      syncReferenceUploadUi({
        errorText: "",
      });
      const uploaded = await uploadReferenceFile(file);
      state.referenceImages.push(uploaded);
      renderReferenceThumbs();
      syncReferenceUploadUi();
    }
  } catch (err) {
    syncReferenceUploadUi({ errorText: "上传失败" });
    showError(err.message || "参考图上传失败");
  } finally {
    state.isUploadingReference = false;
    syncReferenceUploadUi();
  }
}

function parseManualReferenceUrls(value) {
  const urls = [];
  for (const item of String(value || "").split(/[,，]/)) {
    const url = item.trim();
    if (url && !urls.includes(url)) urls.push(url);
  }
  return urls;
}

async function addManualReferenceUrl() {
  const imageUrlInput = $("#imageUrl");
  if (!imageUrlInput) return;

  const urls = parseManualReferenceUrls(imageUrlInput.value);
  if (!urls.length) return;
  if (!state.authenticated) {
    openAuthGate({ mode: "login", message: UNAUTHENTICATED_HINT, focus: true });
    return;
  }
  if (state.isUploadingReference) {
    showError("参考图还在上传，请稍候");
    return;
  }

  const existingUrls = new Set(state.referenceImages.map((item) => item.url));
  const pendingUrls = urls.filter((url) => !existingUrls.has(url));
  if (!pendingUrls.length) {
    imageUrlInput.value = "";
    showError("参考图均已添加");
    return;
  }

  const available = MAX_REFERENCE_IMAGES - getTotalReferenceCount();
  if (available <= 0) {
    const msg = `参考图最多 ${MAX_REFERENCE_IMAGES} 张`;
    syncReferenceUploadUi({ errorText: msg });
    showError(msg);
    return;
  }
  const selectedUrls = pendingUrls.slice(0, available);
  const ignoredCount = pendingUrls.length - selectedUrls.length;

  state.isUploadingReference = true;
  syncReferenceUploadUi();
  showError("");

  try {
    for (const url of selectedUrls) {
      const r = await fetch("/api/reference-url/validate", {
        method: "POST",
        headers: getActiveAuthHeaders({
          "Content-Type": "application/json",
        }),
        body: JSON.stringify({ url }),
      });

      const payload = await r.json().catch(() => null);
      if (!r.ok) {
        if (r.status === 401) {
          await handleUnauthorized("登录已失效，请重新登录");
          throw new Error("登录已失效，请重新登录");
        }
        if (r.status === 404) {
          throw new Error("URL 校验接口未生效，请重启服务后再试");
        }
        const detail = payload?.detail;
        throw new Error(detail?.message || detail || `HTTP ${r.status}`);
      }

      const validatedUrl = payload.url || url;
      if (!state.referenceImages.some((item) => item.url === validatedUrl)) {
        state.referenceImages.push({
          url: validatedUrl,
          name: validatedUrl,
          sizeBytes: 0,
        });
      }
    }
    imageUrlInput.value = "";
    renderReferenceThumbs();
    syncReferenceUploadUi();
    showError(ignoredCount ? `最多添加 ${MAX_REFERENCE_IMAGES} 张参考图，其余已忽略` : "");
  } catch (err) {
    syncReferenceUploadUi({ errorText: "校验失败" });
    showError(err.message || "参考图 URL 校验失败");
  } finally {
    state.isUploadingReference = false;
    syncReferenceUploadUi();
  }
}

function renderResults(items, meta, emptyState = null) {
  const wrap = $("#results");
  const empty = $("#emptyHint");
  const entries = normalizeResultEntries(items);
  state.galleryItems = entries;
  wrap.innerHTML = "";
  if (!entries.length) {
    if (state.previewVisible) {
      closePreview({ restoreFocus: false });
    }
    const emptyTitle = emptyState?.title || "这里会显示你的生成结果。";
    const emptyDetail = emptyState?.detail || "在上方写下提示词开始你的第一张画面。";
    renderResultsEmpty(emptyTitle, emptyDetail);
    return;
  }
  empty.classList.add("is-hidden");
  const frag = document.createDocumentFragment();
  entries.forEach((entry, idx) => {
    const url = entry.url;
    const item = document.createElement("div");
    item.className = "item";
    const indexLabel = formatRecentTimestamp(entry.timestamp) || String(idx + 1).padStart(2, "0");
    const promptText = getPromptDisplayText(entry);
    const promptTitle = escapeHtml(promptText);
    const modeLabel = formatModeLabel(entry.mode);
    const resolutionLabel = formatResolutionSummary(entry) || "清晰度未记录";
    const metaTail = entry.responseTimeMs ? `${entry.responseTimeMs}ms` : "点击查看详情";
    const modelLabel = entry.model || "模型未记录";
    item.innerHTML = `
      <button
        type="button"
        class="gallery-card"
        data-preview-index="${idx}"
        aria-label="预览第 ${idx + 1} 张图片"
      >
        <div class="gallery-media">
          <img src="${url}" alt="${promptTitle}" loading="lazy" onerror="this.closest('.gallery-media')?.classList.add('is-broken'); this.remove();" />
          <span class="gallery-media-empty">图片已被清理或丢失</span>
          <span class="gallery-badge">${escapeHtml(modeLabel)}</span>
          <span class="gallery-open">
            全屏预览
            <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="M7 17 17 7" />
              <path d="M8 7h9v9" />
            </svg>
          </span>
        </div>
        <div class="gallery-body">
          <div class="gallery-row">
            <span class="gallery-index">${escapeHtml(indexLabel)}</span>
            <span class="gallery-chip">${escapeHtml(resolutionLabel)}</span>
          </div>
          <p class="gallery-prompt">${escapeHtml(promptText)}</p>
          <div class="gallery-row gallery-row-muted">
            <span>${escapeHtml(modelLabel)}</span>
            <span>${escapeHtml(metaTail)}</span>
          </div>
        </div>
      </button>
    `;
    frag.appendChild(item);
  });
  wrap.appendChild(frag);
  $("#resultMeta").textContent = meta || "";

  if (state.previewVisible) {
    if (!state.galleryItems.length) {
      closePreview({ restoreFocus: false });
    } else {
      state.previewIndex = Math.min(Math.max(state.previewIndex, 0), state.galleryItems.length - 1);
      renderPreviewModal();
    }
  }
}

function setPreviewVisible(visible) {
  const modal = $("#previewModal");
  if (!modal) return;
  state.previewVisible = Boolean(visible);
  modal.classList.toggle("show", state.previewVisible);
  modal.setAttribute("aria-hidden", state.previewVisible ? "false" : "true");
  syncModalBodyState();
}

function resetPreviewCopyState({ clearText = false } = {}) {
  if (state.previewCopyResetTimer) {
    window.clearTimeout(state.previewCopyResetTimer);
    state.previewCopyResetTimer = null;
  }
  const btn = $("#previewCopyBtn");
  const text = $("#previewCopyBtnText");
  if (btn) {
    if (clearText) {
      btn.dataset.copyText = "";
      btn.disabled = true;
    }
    btn.classList.remove("is-copied");
  }
  if (text) {
    text.textContent = "复制提示词";
  }
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

async function copyPreviewPrompt() {
  const btn = $("#previewCopyBtn");
  const text = $("#previewCopyBtnText");
  const copyValue = String(btn?.dataset.copyText || "").trim();
  if (!copyValue || !btn || !text) return;

  try {
    await copyTextToClipboard(copyValue);
    resetPreviewCopyState();
    btn.classList.add("is-copied");
    text.textContent = "已复制";
    state.previewCopyResetTimer = window.setTimeout(() => {
      resetPreviewCopyState();
    }, 1600);
  } catch (_) {
    text.textContent = "复制失败";
    state.previewCopyResetTimer = window.setTimeout(() => {
      resetPreviewCopyState();
    }, 1600);
  }
}

function renderPreviewModal() {
  const entry = state.galleryItems[state.previewIndex];
  if (!entry) {
    closePreview({ restoreFocus: false });
    return;
  }

  const promptText = getPromptDisplayText(entry);
  const rawPromptText = String(entry.promptPreview || "").trim();
  const modeText = formatModeLabel(entry.mode);
  const resolutionText = entry.resolution || "未记录";
  const aspectRatioText = entry.aspectRatio || "未记录";
  const modelText = entry.model || "未记录";
  const timestampText = formatRecentTimestamp(entry.timestamp) || "刚刚";
  const responseTimeText = entry.responseTimeMs ? `耗时 ${entry.responseTimeMs}ms` : "耗时未记录";
  const total = state.galleryItems.length;
  const prevBtn = $("#previewPrevBtn");
  const nextBtn = $("#previewNextBtn");
  const navDisabled = total <= 1;
  const copyBtn = $("#previewCopyBtn");

  const previewImg = $("#previewImage");
  const previewEmpty = $("#previewEmptyState");
  if (previewImg) {
    previewImg.onerror = () => {
      previewImg.classList.add("is-hidden");
      if (previewEmpty) previewEmpty.classList.remove("is-hidden");
    };
    previewImg.onload = () => {
      if (previewEmpty) previewEmpty.classList.add("is-hidden");
    };
  }
  if (previewImg.getAttribute("src") !== entry.url) {
    // 上一张图片加载失败时会给 img 留下 is-hidden，换图时必须复位，否则后续能正常加载的图片也一直不可见
    previewImg.classList.remove("is-hidden");
    if (previewEmpty) previewEmpty.classList.add("is-hidden");
  }
  previewImg.src = entry.url;
  previewImg.alt = promptText;
  $("#previewCounter").textContent = `${state.previewIndex + 1} / ${total}`;
  $("#previewPrompt").textContent = promptText;
  $("#previewModeChip").textContent = modeText;
  $("#previewModelChip").textContent = modelText;
  $("#previewAspectChip").textContent = aspectRatioText;
  $("#previewAspectChip").classList.toggle("is-hidden", entry.mode === "img2img");
  $("#previewResolutionChip").textContent = resolutionText;
  $("#previewResponseTime").textContent = responseTimeText;
  $("#previewTimestamp").textContent = timestampText;

  prevBtn.disabled = navDisabled;
  nextBtn.disabled = navDisabled;
  resetPreviewCopyState();
  if (copyBtn) {
    copyBtn.dataset.copyText = rawPromptText;
    copyBtn.disabled = !rawPromptText;
  }
}

function openPreview(index) {
  if (!Number.isInteger(index) || index < 0 || index >= state.galleryItems.length) return;
  if (!state.previewVisible && document.activeElement instanceof HTMLElement) {
    state.previewReturnFocus = document.activeElement;
  }
  state.previewIndex = index;
  renderPreviewModal();
  setPreviewVisible(true);
  window.setTimeout(() => {
    $("#previewModalClose")?.focus({ preventScroll: true });
  }, 40);
}

function closePreview({ restoreFocus = true } = {}) {
  if (!state.previewVisible && state.previewIndex < 0) return;
  setPreviewVisible(false);
  state.previewIndex = -1;
  const image = $("#previewImage");
  if (image) {
    image.removeAttribute("src");
    image.removeAttribute("alt");
    image.classList.remove("is-hidden");
  }
  resetPreviewCopyState({ clearText: true });
  const returnFocusEl = state.previewReturnFocus;
  state.previewReturnFocus = null;
  if (
    restoreFocus &&
    returnFocusEl &&
    typeof returnFocusEl.focus === "function" &&
    document.body.contains(returnFocusEl)
  ) {
    returnFocusEl.focus({ preventScroll: true });
  }
}

function stepPreview(delta) {
  if (!state.previewVisible || !state.galleryItems.length) return;
  const total = state.galleryItems.length;
  state.previewIndex = (state.previewIndex + delta + total) % total;
  renderPreviewModal();
}

function bindPreviewModal() {
  const results = $("#results");
  const modal = $("#previewModal");
  const closeBtn = $("#previewModalClose");
  const prevBtn = $("#previewPrevBtn");
  const nextBtn = $("#previewNextBtn");
  const copyBtn = $("#previewCopyBtn");
  if (!results || !modal || !closeBtn || !prevBtn || !nextBtn || !copyBtn) return;

  results.addEventListener("click", (e) => {
    const trigger = e.target.closest("[data-preview-index]");
    if (!trigger) return;
    const idx = Number(trigger.dataset.previewIndex);
    if (!Number.isNaN(idx)) {
      openPreview(idx);
    }
  });

  closeBtn.addEventListener("click", () => closePreview());
  prevBtn.addEventListener("click", () => stepPreview(-1));
  nextBtn.addEventListener("click", () => stepPreview(1));
  copyBtn.addEventListener("click", copyPreviewPrompt);
  modal.addEventListener("click", (e) => {
    if (e.target === e.currentTarget) {
      closePreview();
    }
  });
}

function scrollToLatestResults() {
  const section = $("#resultsSection");
  const target = $("#resultsSection .results-head") || section;
  if (!target) return;

  window.requestAnimationFrame(() => {
    const rect = target.getBoundingClientRect();
    const alreadyVisible = rect.top >= 72 && rect.top < window.innerHeight * 0.35;
    if (alreadyVisible) return;
    target.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  });
}

async function loadRecentImages({ metaText = "", quiet = true } = {}) {
  if (!state.authenticated) {
    renderResults([], "", {
      title: "登录后可查看最近生成的图片。",
      detail: "生成记录会保存在服务器，并按最新时间排在最前面。",
    });
    return [];
  }

  try {
    const r = await fetch(`/api/recent-images?limit=${RECENT_IMAGES_LIMIT}`, {
      headers: getActiveAuthHeaders(),
    });
    if (r.status === 401) {
      await handleUnauthorized("登录已失效，请重新登录");
      return null;
    }
    if (!r.ok) {
      throw new Error(`HTTP ${r.status}`);
    }
    const data = await r.json().catch(() => ({}));
    const items = Array.isArray(data.items) ? data.items : [];
    const finalMeta = metaText || (items.length ? `最近 ${items.length} 张` : "");
    renderResults(items, finalMeta, {
      title: "这里会显示你生成过的图片。",
      detail: "最新生成的图片会自动排在最前面。",
    });
    return items;
  } catch (err) {
    console.error("[recent-images] load failed:", err);
    if (!quiet) {
      showError("最近图片加载失败，请稍后重试");
    }
    return null;
  }
}

async function generate() {
  if (state.isGenerating) return;

  showError("");
  if (!state.authenticated) {
    openAuthGate({ mode: "login", message: UNAUTHENTICATED_HINT, focus: true });
    return;
  }
  const prompt = $("#prompt").value.trim();
  const model = $("#model").value;
  const manualImageUrl = getManualReferenceUrl();
  const requestMode = state.mode;
  const isGptImage2 = requestMode === "text2img" && model === GPT_IMAGE_2_MODEL;
  const requestAspectRatio =
    requestMode === "img2img"
      ? IMG2IMG_PREVIEW_DEFAULT_ASPECT_RATIO
      : isGptImage2
        ? ""
        : $("#aspectRatio").value;
  const requestResolution =
    requestMode === "img2img"
      ? IMG2IMG_PREVIEW_DEFAULT_RESOLUTION
      : isGptImage2
        ? ""
        : $("#resolution").value;
  const requestSize = isGptImage2 ? $("#aspectRatio").value : "";
  const requestQuality = isGptImage2 ? $("#resolution").value : "";

  if (!prompt) {
    showError("请输入提示词");
    return;
  }
  if (!model) {
    showError("请选择模型");
    return;
  }
  if (state.isUploadingReference) {
    showError("参考图还在上传，请稍候");
    return;
  }
  if (requestMode === "img2img" && manualImageUrl) {
    showError("请按回车将参考图 URL 加入为缩略图后再生成");
    return;
  }
  if (requestMode === "img2img" && getTotalReferenceCount() === 0) {
    showError("图生图需要上传参考图或填写参考图 URL");
    return;
  }
  if (requestMode === "img2img" && getTotalReferenceCount() > MAX_REFERENCE_IMAGES) {
    showError(`参考图最多 ${MAX_REFERENCE_IMAGES} 张`);
    return;
  }

  const body = {
    prompt,
    model,
    mode: requestMode,
    aspect_ratio: requestAspectRatio,
    resolution: requestResolution,
    size: requestSize,
    quality: requestQuality,
    image_url: null,
    image_urls: requestMode === "img2img" ? state.referenceImages.map((item) => item.url) : [],
  };

  state.isGenerating = true;
  setLoading(true);
  setProgress({ visible: true, label: "连接中…", fill: 5 });
  state.abortCtrl = new AbortController();
  const t0 = Date.now();
  let timerId = null;
  let shouldRefreshQuota = false;
  // 进度状态：从上游 progress_data 里提取节点进度
  const stage = {
    label: "连接中…",
    fill: 5,
    enteredUpstream: false,
  };
  const metaParts = {};

  function pushStage(nextLabel, nextFill) {
    stage.label = nextLabel;
    if (typeof nextFill === "number") stage.fill = nextFill;
    setProgress({ visible: true, label: stage.label, fill: stage.fill });
  }

  try {
    const r = await fetch("/api/generate/stream", {
      method: "POST",
      headers: getActiveAuthHeaders({
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      }),
      body: JSON.stringify(body),
      signal: state.abortCtrl.signal,
    });

    if (!r.ok || !r.body) {
      // 初始响应就带错（例如 422 参数错误）
      let msg = `HTTP ${r.status}`;
      try {
        const j = await r.json();
        if (Array.isArray(j?.detail)) {
          const reasons = j.detail.map((item) => {
            const field = Array.isArray(item?.loc)
              ? item.loc.filter((part) => !["body", "query", "path"].includes(String(part))).join(".")
              : "";
            const label = field === "prompt" ? "提示词" : field;
            return `${label ? `${label}：` : ""}${item?.msg || "参数校验失败"}`;
          });
          msg = reasons.filter(Boolean).join("；") || msg;
        } else {
          msg = j?.detail?.message || (typeof j?.detail === "string" ? j.detail : msg);
        }
      } catch (_) {}
      if (r.status === 401) {
        await handleUnauthorized("登录已失效，请重新登录");
      }
      if (r.status === 429) showError(msg || "当前繁忙，请稍后手动重试", { autoHide: false });
      throw new Error(msg);
    }
    shouldRefreshQuota = state.authKind === "user";

    // 右侧计时器只显示耗时；进度条填充交由 progress_data 推进
    timerId = setInterval(() => {
      const elapsedSec = ((Date.now() - t0) / 1000).toFixed(1);
      $("#progressTimer").textContent = `${elapsedSec}s`;
    }, 200);

    const reader = r.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    let completed = false;
    let errored = null;

    function consumeSseBlock(block) {
      const dataLines = [];
      for (const line of block.split(/\r?\n/)) {
        if (line.startsWith("data: ")) dataLines.push(line.slice(6));
        else if (line.startsWith("data:")) dataLines.push(line.slice(5));
      }
      if (!dataLines.length) return;
      let evt;
      try {
        evt = JSON.parse(dataLines.join("\n"));
      } catch (_) {
        return;
      }
      if (evt.type === "start") {
        pushStage("已连接上游，准备调度工作流…", 10);
      } else if (evt.type === "progress") {
        const total = Math.max(0, Number(evt.total) || 0);
        const started = Math.max(0, Number(evt.started) || 0);
        if (total > 0) {
          const fillPct = Math.min(94, Math.round(10 + (started / total) * 80));
          pushStage(
            `[${Math.min(started, total)}/${total}] 生成处理中…`,
            Math.max(stage.fill, fillPct),
          );
          stage.enteredUpstream = true;
        }
      } else if (evt.type === "result_pending") {
        pushStage("获取生成结果…", 96);
      } else if (evt.type === "complete") {
        completed = true;
        metaParts.upstream_ms = evt.response_time_ms;
        metaParts.images = evt.images || [];
      } else if (evt.type === "error") {
        errored = evt;
      }
    }

    while (true) {
      const { value, done } = await readSseChunkWithIdleTimeout(
        reader,
        state.abortCtrl,
        GENERATE_STREAM_IDLE_TIMEOUT_MS
      );
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // SSE 以 \n\n 分隔事件块
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        consumeSseBlock(block);
      }
    }

    // 某些代理在响应结束时会保留最后一个没有空行分隔的 SSE 帧。
    const finalBlock = buf.replace(/^\r?\n+|\r?\n+$/g, "");
    if (finalBlock) consumeSseBlock(finalBlock);

    if (errored) {
      // 详细信息打到控制台供调试，banner 只展示后端给的精简 message
      console.error("[generate] request error:", errored.status_code, errored.message);
      if (errored.status_code === 401) {
        await handleUnauthorized("登录已失效，请重新登录");
      }
      if (Number(errored.status_code) === 429) {
        showError(errored.message || "当前繁忙，请稍后手动重试", { autoHide: false });
      }
      throw new Error(errored.message || `生成失败 (${errored.status_code || "?"})`);
    }
    if (!completed) {
      throw new Error("连接中断，未收到完整响应");
    }

    const elapsed = Date.now() - t0;
    const meta = `刚刚更新 · 耗时 ${elapsed}ms`;
    pushStage("完成", 100);
    clearPromptInput({ focus: false });
    const recentItems = await loadRecentImages({ metaText: meta, quiet: true });
    if (recentItems === null) {
      renderResults(
        (metaParts.images || []).map((imageUrl) => ({
          image_url: imageUrl,
          prompt_preview: prompt,
          mode: requestMode,
          model,
          aspect_ratio: isGptImage2 ? requestSize : requestAspectRatio,
          resolution: isGptImage2 ? requestQuality : requestResolution,
          size: requestSize,
          quality: requestQuality,
          response_time_ms: elapsed,
        })),
        meta,
        {
          title: "这里会显示你生成过的图片。",
          detail: "最新生成的图片会自动排在最前面。",
        }
      );
    }
    scrollToLatestResults();


    // 推迟隐藏进度区
    setTimeout(() => setProgress({ visible: false }), 800);
  } catch (err) {
    if (err.name === "StreamIdleTimeoutError") {
      showError(err.message || "长时间未收到生成进度更新，请重试", { autoHide: false });
    } else if (err.name === "AbortError") {
      showError("已取消", { autoHide: false });
    } else {
      // banner 直接展示后端给的精简错误（红色样式已传达失败语义，无需"生成失败："前缀）
      showError(err.message || "生成失败", { autoHide: false });
    }
    setProgress({ visible: false });
  } finally {
    if (timerId) clearInterval(timerId);
    state.abortCtrl = null;
    state.isGenerating = false;
    setLoading(false);
    if (shouldRefreshQuota) {
      scheduleUserQuotaRefresh();
    }
  }
}

function syncPromptComposer() {
  const ta = $("#prompt");
  const counter = $("#promptCount");
  const clearBtn = $("#promptClearBtn");
  if (!ta) return;
  if (counter) counter.textContent = ta.value.length;
  if (clearBtn) clearBtn.classList.toggle("is-hidden", ta.value.length === 0);
}

function clearPromptInput({ focus = false } = {}) {
  const ta = $("#prompt");
  if (!ta) return;
  ta.value = "";
  syncPromptComposer();
  if (focus) {
    ta.focus({ preventScroll: true });
  }
}

function bindPromptUx() {
  const ta = $("#prompt");
  const clearBtn = $("#promptClearBtn");
  if (!ta) return;
  ta.addEventListener("input", syncPromptComposer);
  clearBtn?.addEventListener("click", () => {
    if (!ta.value) return;
    ta.value = "";
    syncPromptComposer();
    ta.focus();
  });
  // ⌘ ↵ / Ctrl+↵ 快捷生成
  ta.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      generate();
    }
  });
  syncPromptComposer();
}

function bindReferenceUpload() {
  const fileInput = $("#referenceImageFile");
  const row = $("#img2imgUploadRow");
  const thumbs = $("#referenceThumbs");
  const imageUrlInput = $("#imageUrl");
  if (!fileInput || !row || !thumbs || !imageUrlInput) return;

  syncReferenceUploadUi();
  renderReferenceThumbs();

  const openPicker = () => {
    if (!state.authenticated) {
      openAuthGate({ mode: "login", message: UNAUTHENTICATED_HINT, focus: true });
      return;
    }
    if (!state.isUploadingReference) {
      fileInput.click();
    }
  };

  row.addEventListener("click", openPicker);
  row.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openPicker();
    }
  });

  fileInput.addEventListener("change", async () => {
    await uploadReferenceFiles(fileInput.files);
    fileInput.value = "";
  });

  thumbs.addEventListener("click", (e) => {
    const removeBtn = e.target.closest(".reference-thumb-remove");
    if (!removeBtn) return;
    const idx = Number(removeBtn.dataset.index);
    if (Number.isNaN(idx)) return;
    state.referenceImages.splice(idx, 1);
    renderReferenceThumbs();
    syncReferenceUploadUi();
    if ($("#errorBox") && !$("#errorBox").classList.contains("is-hidden")) {
      showError("");
    }
  });

  imageUrlInput.addEventListener("input", () => {
    if ($("#errorBox") && !$("#errorBox").classList.contains("is-hidden")) {
      showError("");
    }
  });

  imageUrlInput.addEventListener("keydown", async (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    await addManualReferenceUrl();
  });
}

document.addEventListener("DOMContentLoaded", () => {
  // 先增强 select（此时 options 还是空的；fillSelect 之后由 MutationObserver 同步显示）
  if (window.STImagen) {
    window.STImagen.bindThemeToggle();
    window.STImagen.enhanceAllSelects();
  }

  $$("#authTabs .auth-tab").forEach((btn) => {
    btn.addEventListener("click", () => switchAuthMode(btn.dataset.authMode));
  });
  $("#authEntryBtn").addEventListener("click", () => {
    openAuthGate({ mode: "login", focus: true, animate: true });
  });
  $("#authModalClose").addEventListener("click", closeAuthGate);
  $("#authModal").addEventListener("click", (e) => {
    if (e.target === e.currentTarget) closeAuthGate();
  });
  $("#authLoginBtn").addEventListener("click", submitLogin);
  $("#authActivateBtn").addEventListener("click", submitInviteRegistration);
  $("#authLinuxdoBtn").addEventListener("click", () => startLinuxdoLogin(""));
  $("#authLinuxdoInviteBtn").addEventListener("click", submitInviteRegistration);
  $("#userLogoutBtn").addEventListener("click", logoutUser);
  $("#authLoginUsername").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("#authLoginPassword")?.focus();
  });
  $("#authLoginPassword").addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitLogin();
  });
  $("#authInviteCode").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    submitInviteRegistration();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.previewVisible) {
      closePreview();
      return;
    }
    if (state.previewVisible && e.key === "ArrowLeft") {
      e.preventDefault();
      stepPreview(-1);
      return;
    }
    if (state.previewVisible && e.key === "ArrowRight") {
      e.preventDefault();
      stepPreview(1);
      return;
    }
    if (e.key === "Escape" && state.authGateVisible) {
      closeAuthGate();
    }
  });

  bindModeTabs();
  consumeAuthErrorParam();
  $("#model").addEventListener("change", () => {
    if (state.mode === "text2img") applyText2imgDimensionOptions();
  });
  bindPromptUx();
  bindPreviewModal();
  bindReferenceUpload();
  loadOptions();
  loadAuthStatus();
  $("#generateBtn").addEventListener("click", generate);
});
