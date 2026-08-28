/* Admin console: Overview. Loaded in dependency order by admin.html. */
function deriveMetrics() {
  const accounts = state.accounts;
  const users = state.users;
  const invites = state.invites;
  const logs = state.logs;
  const overview = state.overview || {};

  const totalAccounts = accounts.length || overview.accounts?.total || 0;
  const listedActiveAccounts = accounts.length
    ? accounts.filter((item) => item.status === "active").length
    : overview.accounts?.active || 0;
  const activeAccountItems = accounts.filter((item) => item.status === "active");
  const accountSlotsUsed = sumBy(activeAccountItems, (item) => item.in_flight);
  const accountSlotsTotal = sumBy(activeAccountItems, (item) => item.max_inflight);
  const saturatedAccounts = accounts.filter((item) => computeAccountLoadMeta(item).label === "已打满").length;
  const runtimeMetrics = state.runtimeMetrics || {};
  const configuredGlobalSlots = Number(state.runtimeConfig?.generation?.global_max_concurrent);
  const runtimeGate = {
    ...(Number.isFinite(configuredGlobalSlots) ? { max_concurrent: configuredGlobalSlots, total_slots: configuredGlobalSlots } : {}),
    ...(state.runtimeStatus?.guard?.generation_admission || {}),
    ...(runtimeMetrics.generation || {}),
  };
  const runtimeAccount = runtimeMetrics.account;
  const liveAccountSlotsUsed = Number.isFinite(Number(runtimeAccount?.in_flight))
    ? Number(runtimeAccount.in_flight)
    : accountSlotsUsed;
  const globalSlotsUsed = Number.isFinite(Number(runtimeGate?.in_flight))
    ? Number(runtimeGate.in_flight)
    : 0;
  // Global generation capacity is a separate process-wide limit. Never use
  // the account-pool total as its fallback (they are different capacities).
  const globalSlotsTotal = Number.isFinite(Number(runtimeGate?.max_concurrent))
    ? Number(runtimeGate.max_concurrent)
    : 0;
  const activeAccounts = listedActiveAccounts;
  const disabledAccounts = Math.max(0, totalAccounts - activeAccounts);

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
  const persistedGeneratedImages = Number(overview.generations?.images_total);
  const totalGeneratedImages = Number.isFinite(persistedGeneratedImages)
    ? Math.max(0, persistedGeneratedImages)
    : logs.reduce(
        (total, item) =>
          total + (item.status === "success" ? parseOutputImages(item.output_images, item.output_preview).length : 0),
        0,
      );
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
    runtimeAccountSlotsUsed: liveAccountSlotsUsed,
    globalSlotsUsed,
    globalSlotsTotal,
    runtimeAccountSlotsTotal: accountSlotsTotal,
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
    totalGeneratedImages,
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

function svgPolyline(values, width, height, padding, maxValue) {
  const usableWidth = width - padding.left - padding.right;
  const usableHeight = height - padding.top - padding.bottom;
  const step = values.length > 1 ? usableWidth / (values.length - 1) : usableWidth;
  return values.map((value, index) => {
    const x = padding.left + index * step;
    const y = padding.top + usableHeight - (Number(value || 0) / Math.max(1, maxValue)) * usableHeight;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
}

function renderRecentActivity(dashboard) {
  const el = $("#recentActivity");
  if (!el) return;
  if (!dashboard.recentActivity.length) {
    el.innerHTML = '<div class="admin-empty-state"><span>暂无生成活动。</span></div>';
    return;
  }
  el.innerHTML = dashboard.recentActivity.map((log) => `<div class="activity-item"><span class="activity-status ${log.status === "success" ? "success" : "error"}">${log.status === "success" ? "✓" : "!"}</span><div class="activity-copy"><strong>${escapeHtml(log.model || humanMode(log.mode))}</strong><span>${escapeHtml(log.username || "匿名用户")} · ${escapeHtml(fmtRelativeTime(log.timestamp))}${log.error_message ? ` · ${escapeHtml(truncateText(log.error_message, 34))}` : ""}</span></div><span class="mono table-note">${escapeHtml(fmtDuration(log.response_time_ms))}</span></div>`).join("");
}

function dashboardPeriodLabel(period = state.dashboardPeriod) {
  return period === "7d" ? "最近 7 天" : period === "30d" ? "最近 30 天" : "最近 24 小时";
}

function normalizeLiveModelKey(value) {
  const token = String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  if (["gpt image 2", "gptimage2"].includes(token)) return "gpt_image_2";
  if (["nano banana pro", "gemini 3 pro image preview", "gemini 3 pro image"].includes(token)) return "nano_banana_pro";
  return "other";
}

function liveModelCount(models, target) {
  return Object.entries(models || {}).reduce((sum, [model, snapshot]) => {
    return sum + (normalizeLiveModelKey(model) === target ? Number(snapshot?.in_flight || 0) : 0);
  }, 0);
}

function renderCapacityPanel() {
  const configured = Number(state.runtimeConfig?.generation?.global_max_concurrent);
  const hasCapacity = Number.isFinite(configured) && configured > 0;
  const inFlight = Number(state.runtimeMetrics?.generation?.in_flight || 0);
  const percent = hasCapacity ? Math.min(100, Math.max(0, (inFlight / configured) * 100)) : 0;
  const activeAccounts = state.accounts.filter((item) => item.status === "active").length || Number(state.overview?.accounts?.active || 0);
  const accountInFlight = Number(state.runtimeMetrics?.account?.in_flight ?? sumBy(state.accounts, (item) => item.in_flight));
  const accountTotal = sumBy(state.accounts.filter((item) => item.status === "active"), (item) => item.max_inflight);
  const status = !hasCapacity ? { label: "容量未配置", tone: "warning", state: "当前进程未配置全局容量，暂不显示虚假占用比例。" }
    : inFlight >= configured ? { label: "容量已满", tone: "danger", state: "全局并发已达到上限，新请求将按准入策略处理。" }
      : percent >= 80 ? { label: "接近上限", tone: "warning", state: "当前运行中，请关注剩余可用容量。" }
        : inFlight > 0 ? { label: "运行中", tone: "success", state: "有生图请求正在处理。" }
          : { label: "空闲", tone: "success", state: "当前没有在途生图请求。" };
  const badge = $("#capacityStatusBadge");
  const inFlightEl = $("#capacityInFlight");
  const totalEl = $("#capacityTotal");
  const stateEl = $("#capacityStateText");
  const percentEl = $("#capacityPercent");
  const progress = $("#capacityProgress");
  if (badge) {
    badge.className = `capacity-status ${status.tone}`;
    badge.textContent = status.label;
  }
  if (inFlightEl) inFlightEl.textContent = hasCapacity ? fmtNumber(inFlight) : "—";
  if (totalEl) totalEl.textContent = hasCapacity ? fmtNumber(configured) : "—";
  if (stateEl) stateEl.textContent = status.state;
  if (percentEl) percentEl.textContent = hasCapacity ? `${percent.toFixed(0)}%` : "—";
  if (progress) {
    progress.className = `capacity-progress ${hasCapacity ? status.tone : "unavailable"}`;
    progress.setAttribute("aria-valuenow", String(Math.round(percent)));
    const fill = progress.querySelector("span");
    if (fill) fill.style.width = `${percent}%`;
  }
  const models = state.runtimeMetrics?.generation?.models || {};
  const liveGpt = $("#liveGptImage2");
  const liveNano = $("#liveNanoBanana");
  if (liveGpt) liveGpt.textContent = fmtNumber(liveModelCount(models, "gpt_image_2"));
  if (liveNano) liveNano.textContent = fmtNumber(liveModelCount(models, "nano_banana_pro"));
  const activeEl = $("#capacityActiveAccounts");
  const accountInFlightEl = $("#capacityAccountInFlight");
  const accountTotalEl = $("#capacityAccountTotal");
  if (activeEl) activeEl.textContent = fmtNumber(activeAccounts);
  if (accountInFlightEl) accountInFlightEl.textContent = fmtNumber(accountInFlight);
  if (accountTotalEl) accountTotalEl.textContent = fmtNumber(accountTotal);
  const updated = $("#capacityUpdatedAt");
  if (updated) updated.textContent = state.runtimeMetricsUpdatedAt ? `更新于 ${fmtTime(state.runtimeMetricsUpdatedAt)}` : "实时轮询 · 5 秒";
}

function renderProtectionStatus() {
  const el = $("#protectionStatus");
  if (!el) return;
  const runtime = state.runtimeStatus;
  if (!runtime) {
    el.innerHTML = '<div class="admin-empty-state"><span>保护状态同步中…</span></div>';
    return;
  }
  const breaker = runtime.guard?.circuit_breaker;
  const routes = Object.values(runtime.guard?.route_breakers || {});
  const openRoutes = routes.filter((item) => item?.state === "open" || item?.is_open).length;
  const probingRoutes = routes.filter((item) => item?.state === "half-open" || item?.is_half_open).length;
  const isolations = Array.isArray(runtime.account_isolations) ? runtime.account_isolations.length : 0;
  const rows = [
    { label: "熔断器", value: !breaker?.enabled ? "未启用" : breaker.is_open ? "已断开" : "正常", note: breaker?.is_open ? `剩余 ${formatRuntimeSeconds(breaker.remaining_seconds)}` : "上游故障保护", tone: breaker?.is_open ? "danger" : !breaker?.enabled ? "warning" : "", actionLabel: "查看日志", actionPage: "logs" },
    { label: "路由状态", value: routes.length ? `${routes.length - openRoutes - probingRoutes} / ${routes.length}` : "无记录", note: openRoutes ? `${openRoutes} 条已打开${probingRoutes ? ` · ${probingRoutes} 条探测中` : ""}` : probingRoutes ? `${probingRoutes} 条探测中` : "全部路由正常", tone: openRoutes ? "danger" : probingRoutes ? "warning" : "", actionLabel: "查看日志", actionPage: "logs" },
    { label: "账号隔离", value: `${isolations} 个`, note: isolations ? "隔离账号不会接收新的生成请求" : "当前无故障隔离账号", tone: isolations ? "danger" : "", actionLabel: "管理账号", actionPage: "accounts" },
  ];
  el.innerHTML = rows.map((row) => `<div class="protection-item ${row.tone}"><span class="protection-dot"></span><div class="protection-copy"><strong>${escapeHtml(row.label)}</strong><span>${escapeHtml(row.note)}</span></div><span class="protection-value">${escapeHtml(row.value)}</span><a class="protection-action" href="${buildAdminPageHref(row.actionPage)}" data-protection-page="${escapeHtml(row.actionPage)}">${escapeHtml(row.actionLabel)}</a></div>`).join("");
}

function renderAnalyticsTrend(analytics) {
  const el = $("#hourlyTrendChart");
  const legend = $("#trendLegend");
  const accessibleSummary = $("#trendAccessibleSummary");
  const timeline = Array.isArray(analytics?.timeline) ? analytics.timeline : [];
  if (!el) return;
  if (!analytics || !timeline.length || !Number(analytics.summary?.requests || 0)) {
    const emptyLabel = state.dashboardAnalyticsLoading || state.refreshing ? "分析数据加载中…" : "当前周期暂无生成请求。";
    el.innerHTML = `<div class="admin-empty-state"><span>${emptyLabel}</span></div>`;
    el.setAttribute("aria-busy", state.dashboardAnalyticsLoading ? "true" : "false");
    if (accessibleSummary) accessibleSummary.textContent = emptyLabel;
    if (legend) legend.innerHTML = "";
    return;
  }
  const series = timeline.map((item) => ({
    ...item,
    total: Number(item.requests || 0),
    error: Number(item.failure || 0),
  }));
  const width = 760;
  const height = 218;
  const padding = { top: 12, right: 8, bottom: 28, left: 8 };
  const maxValue = Math.max(...series.map((item) => item.total), 1);
  const totalPoints = svgPolyline(series.map((item) => item.total), width, height, padding, maxValue);
  const successPoints = svgPolyline(series.map((item) => item.success), width, height, padding, maxValue);
  const errorPoints = svgPolyline(series.map((item) => item.error), width, height, padding, maxValue);
  const areaPoints = `${padding.left},${height - padding.bottom} ${totalPoints} ${width - padding.right},${height - padding.bottom}`;
  const labelIndexes = series.length <= 7 ? series.map((_, index) => index) : [0, Math.floor((series.length - 1) / 3), Math.floor((series.length - 1) * 2 / 3), series.length - 1];
  const labelStep = series.length > 1 ? (width - padding.left - padding.right) / (series.length - 1) : 0;
  const labels = labelIndexes.map((index) => {
    const item = series[index];
    const label = formatInShanghai(item.start, analytics.period === "24h" ? { hour: "2-digit" } : { month: "2-digit", day: "2-digit" });
    return `<text x="${(padding.left + index * labelStep).toFixed(1)}" y="${height - 6}" text-anchor="middle" class="trend-axis-label">${escapeHtml(label)}</text>`;
  }).join("");
  const grid = [0, 1, 2, 3].map((index) => {
    const y = padding.top + ((height - padding.top - padding.bottom) / 3) * index;
    return `<line x1="${padding.left}" y1="${y.toFixed(1)}" x2="${width - padding.right}" y2="${y.toFixed(1)}" class="trend-grid-line" />`;
  }).join("");
  el.setAttribute("aria-label", `${dashboardPeriodLabel(analytics.period)}生成请求趋势，共 ${fmtNumber(analytics.summary.requests)} 次请求`);
  el.setAttribute("aria-busy", state.dashboardAnalyticsLoading ? "true" : "false");
  if (accessibleSummary) accessibleSummary.textContent = `${dashboardPeriodLabel(analytics.period)}共 ${fmtNumber(analytics.summary.requests)} 次请求，其中成功 ${fmtNumber(analytics.summary.success)} 次，失败 ${fmtNumber(analytics.summary.failure)} 次。`;
  el.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">${grid}<polygon points="${areaPoints}" class="trend-area" /><polyline points="${totalPoints}" class="trend-line" /><polyline points="${successPoints}" class="trend-success-line" /><polyline points="${errorPoints}" class="trend-error-line" />${labels}</svg>`;
  if (legend) legend.innerHTML = '<span><i></i>全部请求</span><span class="success"><i></i>成功</span><span class="error"><i></i>失败</span>';
}

function renderFailureAnalytics(analytics) {
  const summaryEl = $("#failureSummary");
  const chart = $("#failureChart");
  const failures = Array.isArray(analytics?.failures) ? analytics.failures : [];
  const total = Number(analytics?.summary?.failure || 0);
  if (chart) chart.setAttribute("aria-busy", state.dashboardAnalyticsLoading ? "true" : "false");
  if (summaryEl) summaryEl.innerHTML = `<strong>${fmtNumber(total)}</strong><span>周期内失败请求</span>`;
  if (!chart) return;
  if (!total) {
    chart.innerHTML = `<div class="admin-empty-state"><span>${state.dashboardAnalyticsLoading || state.refreshing ? "分析数据加载中…" : "当前周期暂无失败记录。"}</span></div>`;
    return;
  }
  chart.innerHTML = failures.map((item) => `<button class="failure-row ${item.count ? "" : "zero"}" type="button" data-failure-category="${escapeHtml(item.key)}"><span class="failure-row-head"><span>${escapeHtml(item.label)}</span><strong>${fmtNumber(item.count)} · ${Number(item.share || 0).toFixed(1)}%</strong></span><span class="failure-track"><span style="width:${Math.min(100, Math.max(0, Number(item.share || 0)))}%"></span></span></button>`).join("");
  chart.querySelectorAll("[data-failure-category]").forEach((button) => {
    button.addEventListener("click", () => {
      state.filters.logs.status = "error";
      state.filters.logs.category = button.dataset.failureCategory || "all";
      syncFilterInputs("logs");
      showAdminPage("logs", { updateHistory: true });
      renderLogsTable();
    });
  });
}

function renderModelAnalytics(analytics) {
  const el = $("#modelAnalytics");
  if (!el) return;
  el.setAttribute("aria-busy", state.dashboardAnalyticsLoading ? "true" : "false");
  const models = (Array.isArray(analytics?.models) ? analytics.models : [])
    .filter((item) => item.key !== "other");
  const known = ["gpt_image_2", "nano_banana_pro"];
  const ordered = known.map((key) => models.find((item) => item.key === key) || { key, label: key === "gpt_image_2" ? "GPT Image 2" : "Nano Banana Pro", requests: 0, request_share: 0, success: 0, failure: 0, success_rate: 0, avg_response_ms: 0, specs: key === "gpt_image_2" ? [{ label: "Quality", items: [] }, { label: "Size", items: [] }] : [{ label: "分辨率", items: [] }] });
  if (!analytics) {
    el.innerHTML = '<div class="admin-empty-state"><span>模型分析加载中…</span></div>';
    return;
  }
  el.innerHTML = ordered.map((model) => {
    const requestCount = Number(model.requests || 0);
    const specs = (Array.isArray(model.specs) ? model.specs : []).map((group) => ({
      ...group,
      items: (group.items || []).filter((item) => item.key !== "unknown" && !String(item.label || "").includes("未知")),
    }));
    const maxSpecCount = Math.max(1, ...specs.flatMap((group) => (group.items || []).map((item) => Number(item.requests || 0))));
    return `<article class="model-card"><div class="model-card-head"><div><h3>${escapeHtml(model.label)}</h3><p class="model-card-caption">${model.key === "other" ? "未识别模型" : "文生图"}</p></div><span class="model-card-share">${Number(model.request_share || 0).toFixed(1)}%</span></div><div class="model-metrics"><div class="model-metric"><span>请求数</span><strong>${fmtNumber(requestCount)}</strong></div><div class="model-metric"><span>成功数</span><strong>${fmtNumber(model.success)}</strong></div><div class="model-metric failure"><span>失败数</span><strong>${fmtNumber(model.failure)}</strong></div><div class="model-metric"><span>成功率</span><strong>${Number(model.success_rate || 0).toFixed(1)}%</strong></div></div><div class="model-card-foot"><span>平均处理时长</span><strong>${requestCount ? fmtDuration(model.avg_response_ms) : "—"}</strong></div>${specs.length ? `<div class="model-spec-groups">${specs.map((group) => `<div class="model-spec-group"><strong>${escapeHtml(group.label)}</strong>${(group.items || []).map((item) => `<div class="model-spec-row"><span>${escapeHtml(item.label)}</span><div class="model-spec-track"><span style="width:${requestCount ? Math.min(100, (Number(item.requests || 0) / maxSpecCount) * 100) : 0}%"></span></div><strong>${fmtNumber(item.requests)}</strong><span class="model-spec-failure">失败 ${fmtNumber(item.failure)}</span></div>`).join("")}</div>`).join("")}</div>` : ""}</article>`;
  }).join("");
}

function renderDashboardKpis(dashboard) {
  const el = $("#overview");
  if (!el) return;
  if (!dashboard?.period && (state.refreshing || state.dashboardAnalyticsLoading)) {
    el.innerHTML = ["周期内生成请求", "成功率", "平均处理时长"].map((label) => `<div class="stat-card"><p class="stat-label">${label}</p><div class="stat-value">—</div><p class="stat-meta">数据同步中…</p></div>`).join("");
    return;
  }
  const kpi = dashboard?.kpis || {};
  const period = dashboard?.period || state.dashboardPeriod;
  const avgResponse = Number(kpi.avgResponseMs) > 0 ? fmtDuration(kpi.avgResponseMs) : "—";
  el.innerHTML = `
    <div class="stat-card"><p class="stat-label">${dashboardPeriodLabel(period)}生成请求</p><div class="stat-value">${fmtNumber(kpi.requests)}</div><p class="stat-meta">成功 ${fmtNumber(kpi.success)} · 失败 ${fmtNumber(kpi.failure)}</p></div>
    <div class="stat-card"><p class="stat-label">成功率</p><div class="stat-value">${Number(kpi.successRate || 0).toFixed(1)}<span class="stat-value-sub">%</span></div><p class="stat-meta">${dashboardPeriodLabel(period)}完成情况</p></div>
    <div class="stat-card"><p class="stat-label">平均处理时长</p><div class="stat-value">${escapeHtml(avgResponse)}</div><p class="stat-meta">${dashboardPeriodLabel(period)} · 包含成功与失败请求</p></div>`;
}

function renderDashboardPanels(metrics) {
  const analytics = state.dashboardAnalytics;
  const dashboard = {
    period: analytics?.period || null,
    kpis: {
      requests: Number(analytics?.summary?.requests || 0),
      success: Number(analytics?.summary?.success || 0),
      failure: Number(analytics?.summary?.failure || 0),
      successRate: analytics?.summary?.requests ? (Number(analytics.summary.success || 0) / Number(analytics.summary.requests || 1)) * 100 : 0,
      avgResponseMs: Number(analytics?.summary?.avg_response_ms || 0),
      totalGeneratedImages: metrics.totalGeneratedImages,
      activeUsers: metrics.activeUsers,
      totalUsers: metrics.totalUsers,
    },
    recentActivity: [...state.logs].sort((a, b) => (parseApiDate(b.timestamp)?.getTime() || 0) - (parseApiDate(a.timestamp)?.getTime() || 0)).slice(0, 6),
  };
  state.dashboardMetrics = dashboard;
  renderDashboardKpis(dashboard);
  renderCapacityPanel();
  renderAnalyticsTrend(analytics);
  renderFailureAnalytics(analytics);
  renderProtectionStatus();
  renderRecentActivity(dashboard);
  const meta = $("#trendSampleMeta");
  if (meta) meta.textContent = state.dashboardAnalyticsLoading ? `正在加载${dashboardPeriodLabel(state.dashboardPeriod)}…` : analytics ? `${dashboardPeriodLabel(analytics.period)} · 完整日志聚合` : "正在加载完整周期聚合…";
  const analyticsMeta = $("#analyticsPeriodMeta");
  if (analyticsMeta) analyticsMeta.textContent = state.dashboardAnalyticsLoading ? `正在加载${dashboardPeriodLabel(state.dashboardPeriod)} · UTC 聚合` : `${dashboardPeriodLabel(state.dashboardPeriod)} · UTC 聚合`;
  const secondaryMeta = $("#analyticsSecondaryMeta");
  if (secondaryMeta) secondaryMeta.textContent = `累计图片 ${fmtNumber(metrics.totalGeneratedImages)} · 活跃用户 ${fmtNumber(metrics.activeUsers)} / ${fmtNumber(metrics.totalUsers)}`;
  const modelMeta = $("#modelAnalyticsMeta");
  if (modelMeta) modelMeta.textContent = state.dashboardAnalyticsLoading ? `正在加载${dashboardPeriodLabel(state.dashboardPeriod)}…` : analytics ? `${dashboardPeriodLabel(analytics.period)} · 仅文生图` : "正在加载…";
  renderModelAnalytics(analytics);
}

function renderSyncState() {
  const adminBadge = $("#adminIdentityBadge");
  if (adminBadge) {
    adminBadge.textContent = state.admin?.username ? `管理员 · ${state.admin.username}` : "管理员";
  }
  const sync = $("#adminSyncStatus");
  if (sync) {
    if (state.refreshing) sync.textContent = `正在同步 ${state.sync.successCount}/${state.sync.totalCount}`;
    else if (state.sync.status === "partial") sync.textContent = `部分同步 · ${state.sync.successCount}/${state.sync.totalCount}`;
    else if (state.lastUpdatedAt) sync.textContent = `已同步 · ${fmtTime(state.lastUpdatedAt)}`;
    else sync.textContent = "等待同步";
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
    metrics.saturatedAccounts ? `${fmtNumber(metrics.saturatedAccounts)}` : `${fmtNumber(metrics.activeAccounts)}`,
    metrics.saturatedAccounts ? "danger" : metrics.activeAccounts ? "success" : "",
  );
  setNavBadge(
    "navBadgeUsers",
    userRiskCount ? `${fmtNumber(userRiskCount)}` : `${fmtNumber(metrics.activeUsers)}`,
    userRiskCount ? "warning" : metrics.activeUsers ? "success" : "",
  );
  setNavBadge(
    "navBadgeInvites",
    metrics.activeInvites ? `${fmtNumber(metrics.activeInvites)}` : "0",
    metrics.activeInvites ? "success" : "warning",
  );
  setNavBadge(
    "navBadgeLogs",
    metrics.errorGenerations ? `${fmtNumber(metrics.errorGenerations)}` : `${fmtNumber(metrics.successGenerations)}`,
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

// ==================== 运行状态（流量守卫） ====================

function formatRuntimeSeconds(seconds) {
  const value = Math.max(0, Math.ceil(Number(seconds) || 0));
  if (value < 60) return `${value}s`;
  const minutes = Math.floor(value / 60);
  const remaining = value % 60;
  if (minutes < 60) return remaining ? `${minutes}分${remaining}s` : `${minutes}分`;
  const hours = Math.floor(minutes / 60);
  const minutePart = minutes % 60;
  return minutePart ? `${hours}小时${minutePart}分` : `${hours}小时`;
}

function renderOverviewUptime() {
  const el = $("#overviewUptime");
  const blocks = $("#overviewSyncBlocks");
  if (!el) return;
  if (blocks) {
    blocks.textContent = `${state.sync.successCount || 0}/${state.sync.totalCount || 5} 数据块成功`;
  }
  el.classList.remove("hero-pill-success", "hero-pill-danger");
  if (state.refreshing) {
    el.textContent = "同步中";
  } else if (state.sync.status === "partial") {
    el.textContent = "部分失败";
    el.classList.add("hero-pill-danger");
  } else if (state.sync.status === "ready") {
    el.textContent = "已同步";
    el.classList.add("hero-pill-success");
  } else {
    el.textContent = "未同步";
  }
}

let _runtimeMetricsTimer = null;
let _runtimeStatusTimer = null;
let _dashboardRefreshTimer = null;

async function refreshRuntimeMetrics() {
  try {
    state.runtimeMetrics = await api("/api/admin/runtime-metrics");
    state.runtimeMetricsUpdatedAt = new Date();
    renderLiveCapacityPanels();
    return true;
  } catch {
    return false;
  }
}

async function refreshRuntimeStatus() {
  try {
    state.runtimeStatus = await api("/api/admin/runtime-status");
    if (!state.batchRendering) {
      renderLiveCapacityPanels();
      renderProtectionStatus();
    }
    return true;
  } catch (err) {
    const el = $("#protectionStatus");
    if (el) el.innerHTML = `<div class="admin-empty-state"><span>保护状态加载失败：${escapeHtml(err.message)}</span></div>`;
    return false;
  }
}

function startRuntimeMetricsPolling() {
  stopRuntimeMetricsPolling();
  _runtimeMetricsTimer = window.setInterval(() => {
    if (document.hidden) return;
    refreshRuntimeMetrics();
  }, 5000);
}

function stopRuntimeMetricsPolling() {
  if (_runtimeMetricsTimer) {
    window.clearInterval(_runtimeMetricsTimer);
    _runtimeMetricsTimer = null;
  }
}

function startRuntimeStatusPolling() {
  stopRuntimeStatusPolling();
  _runtimeStatusTimer = window.setInterval(() => {
    if (document.hidden) return; // 页面不可见时暂停轮询
    refreshRuntimeStatus();
  }, 30 * 1000);
}

function stopRuntimeStatusPolling() {
  if (_runtimeStatusTimer) {
    window.clearInterval(_runtimeStatusTimer);
    _runtimeStatusTimer = null;
  }
}

async function refreshDashboardLightweight() {
  if (state.refreshing || document.hidden || !getToken()) return;
  const results = await Promise.all([refreshOverview(), refreshLogs(), refreshDashboardAnalytics()]);
  if (results.some(Boolean)) {
    state.lastUpdatedAt = new Date();
    renderInsightPanels();
  }
}

function startDashboardRefreshPolling() {
  stopDashboardRefreshPolling();
  _dashboardRefreshTimer = window.setInterval(refreshDashboardLightweight, 60 * 1000);
}

function stopDashboardRefreshPolling() {
  if (_dashboardRefreshTimer) {
    window.clearInterval(_dashboardRefreshTimer);
    _dashboardRefreshTimer = null;
  }
}

function renderLiveCapacityPanels() {
  if (state.batchRendering) return;
  renderCapacityPanel();
}

function renderInsightPanels() {
  if (state.batchRendering) return;
  const metrics = deriveMetrics();

  renderSyncState();
  renderDashboardPanels(metrics);
  renderOverviewUptime();
  renderNavBadges(metrics);
  renderProtectionStatus();
}
