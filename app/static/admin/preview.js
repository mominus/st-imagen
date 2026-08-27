/* Admin console: Preview. Loaded in dependency order by admin.html. */
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

function syncDashboardPeriodControls() {
  $$(`[data-dashboard-period]`).forEach((button) => {
    const active = button.dataset.dashboardPeriod === state.dashboardPeriod;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
    button.disabled = state.dashboardAnalyticsLoading;
  });
}

async function refreshDashboardSnapshot(period = state.dashboardPeriod) {
  const previousPeriod = state.dashboardPeriod;
  const requestId = state.dashboardAnalyticsRequestId + 1;
  state.dashboardAnalyticsRequestId = requestId;
  state.dashboardAnalyticsAbortController?.abort();
  const controller = new AbortController();
  state.dashboardAnalyticsAbortController = controller;
  state.dashboardPeriod = period;
  state.dashboardAnalyticsLoading = true;
  state.dashboardSnapshotLoading = true;
  syncDashboardPeriodControls();
  renderInsightPanels();

  try {
    const data = await api(`/api/admin/dashboard/snapshot?period=${encodeURIComponent(period)}`, { signal: controller.signal });
    if (requestId !== state.dashboardAnalyticsRequestId) return false;
    state.admin = data.admin || state.admin;
    state.overview = data.overview || null;
    state.dashboardAnalytics = data.analytics || null;
    state.runtimeMetrics = data.runtime_metrics || null;
    state.runtimeStatus = data.runtime_status || null;
    state.runtimeConfig = data.runtime_config || state.runtimeConfig;
    state.runtimeMetricsUpdatedAt = new Date();
    state.logs = Array.isArray(data.recent_logs) ? data.recent_logs : [];
    return true;
  } catch (err) {
    if (requestId !== state.dashboardAnalyticsRequestId || err?.name === "AbortError") return false;
    state.dashboardPeriod = previousPeriod;
    const meta = $("#overviewMeta");
    if (meta) meta.textContent = `驾驶舱快照加载失败，正在使用分接口同步：${err.message}`;
    return false;
  } finally {
    if (requestId === state.dashboardAnalyticsRequestId) {
      state.dashboardAnalyticsLoading = false;
      state.dashboardSnapshotLoading = false;
      state.dashboardAnalyticsAbortController = null;
      syncDashboardPeriodControls();
      renderInsightPanels();
    }
  }
}

async function refreshDashboardAnalytics(period = state.dashboardPeriod) {
  const previousPeriod = state.dashboardPeriod;
  const requestId = state.dashboardAnalyticsRequestId + 1;
  state.dashboardAnalyticsRequestId = requestId;
  state.dashboardAnalyticsAbortController?.abort();
  const controller = new AbortController();
  state.dashboardAnalyticsAbortController = controller;
  state.dashboardPeriod = period;
  state.dashboardAnalyticsLoading = true;
  state.dashboardSnapshotLoading = false;
  syncDashboardPeriodControls();
  renderInsightPanels();

  try {
    const data = await api(`/api/admin/stats/dashboard?period=${encodeURIComponent(period)}`, { signal: controller.signal });
    if (requestId !== state.dashboardAnalyticsRequestId) return false;
    state.dashboardAnalytics = data;
    return true;
  } catch (err) {
    if (requestId !== state.dashboardAnalyticsRequestId || err?.name === "AbortError") return false;
    state.dashboardPeriod = previousPeriod;
    const meta = $("#overviewMeta");
    if (meta) meta.textContent = `分析数据加载失败：${err.message}`;
    return false;
  } finally {
    if (requestId === state.dashboardAnalyticsRequestId) {
      state.dashboardAnalyticsLoading = false;
      state.dashboardAnalyticsAbortController = null;
      syncDashboardPeriodControls();
      renderInsightPanels();
    }
  }
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
  state.sync = { status: "loading", successCount: 0, totalCount: 5 };
  const refreshButton = $("#refreshAllBtn");
  if (refreshButton) refreshButton.disabled = true;
  renderInsightPanels();

  state.batchRendering = true;
  let results;
  try {
    const snapshotReady = await refreshDashboardSnapshot();
    if (snapshotReady) {
      // Paint the operator-facing viewport as soon as the snapshot arrives;
      // management tables can finish loading without holding the cockpit.
      state.batchRendering = false;
      renderLogsTable();
      renderInsightPanels();
      state.batchRendering = true;
      results = [
        true,
        ...(await Promise.all([
          refreshAccounts(),
          refreshUsers(),
          refreshInvites(),
          refreshSettings(),
        ])),
      ];
    } else {
      // Keep the established per-endpoint path as a complete fallback when
      // the aggregate endpoint is unavailable or returns stale data.
      results = await Promise.all([
        refreshAdminProfile(),
        refreshOverview(),
        refreshAccounts(),
        refreshUsers(),
        refreshInvites(),
        refreshLogs(),
        refreshSettings(),
        refreshRuntimeMetrics(),
        refreshRuntimeStatus(),
        refreshDashboardAnalytics(),
      ]);
    }
  } catch (err) {
    results = Array.from({ length: 5 }, () => false);
    showToast(`控制台刷新异常：${err.message}`, "error");
  } finally {
    state.batchRendering = false;
  }

  state.refreshing = false;
  state.lastUpdatedAt = new Date();
  state.sync = {
    status: results.every(Boolean) ? "ready" : "partial",
    successCount: results.filter(Boolean).length,
    totalCount: results.length,
  };
  if (refreshButton) refreshButton.disabled = false;
  renderLogsTable();
  renderInsightPanels();
  if (state.sync.status === "partial") {
    showToast(`控制台已刷新，${state.sync.successCount}/${state.sync.totalCount} 个数据块成功。`, "warning");
  }
}

// ==================== 应用设置 ====================
