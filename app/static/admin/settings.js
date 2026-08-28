/* Admin console: Settings. Loaded in dependency order by admin.html. */
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
  const disk = storage?.disk;
  const diskStat = $("#storageDiskStat");
  const diskHint = $("#storageDiskHint");
  if (diskStat) {
    diskStat.textContent = !disk
      ? "暂不可用"
      : disk.available === false
        ? "空间不足"
        : "正常";
  }
  if (diskHint) {
    diskHint.textContent = !disk
      ? "磁盘探测不可用"
      : `剩余 ${formatBytes(disk.free_bytes)} · 预留 ${formatBytes(disk.min_free_bytes)} · 可写 ${formatBytes(disk.writable_bytes)}`;
  }
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

function renderRuntimeConfig(config) {
  const panel = $("#runtimeConfigPanel");
  const meta = $("#runtimeConfigMeta");
  if (!panel) return;
  if (!config) {
    panel.innerHTML = '<p class="muted">暂不可用</p>';
    if (meta) meta.textContent = "未返回运行参数";
    return;
  }

  const process = config.process || {};
  const generation = config.generation || {};
  const network = config.network || {};
  const persistence = config.persistence || {};
  const formatSeconds = (value) => {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    if (number >= 3600 && number % 3600 === 0) return `${number / 3600}小时`;
    if (number >= 60 && number % 60 === 0) return `${number / 60}分钟`;
    return `${Number.isInteger(number) ? number : number.toFixed(1)}s`;
  };
  const workerLabel = process.single_worker_ok === false
    ? `${fmtNumber(process.worker_count || 0)}（需单 worker）`
    : `${fmtNumber(process.worker_count || 1)}（符合）`;
  const rows = [
    ["生图并发与限速", `${fmtNumber(generation.global_max_concurrent || 0)} 个`, `单账号默认 ${fmtNumber(generation.account_default_max_inflight || 0)} 个 · 用户 ${generation.user_rpm_limit ? `${fmtNumber(generation.user_rpm_limit)} 次/分钟` : "限速关闭"}`],
    ["工作流超时", `无进度 ${formatSeconds(generation.workflow_idle_timeout_seconds)} · 总计 ${formatSeconds(generation.workflow_total_timeout_seconds)}`, `SSE 保活 ${formatSeconds(generation.sse_keepalive_interval_seconds)} · 任一工作流时限达到即终止`],
    ["上游网络超时", `连接 ${formatSeconds(network.upstream_connect_timeout_seconds)} · 读取 ${formatSeconds(network.upstream_read_timeout_seconds)}`, `传输总超时 ${formatSeconds(network.upstream_timeout_seconds)} · 连接池等待也受此限制`],
    ["上游 HTTP 连接池", `${fmtNumber(network.http_max_connections || 0)} 个`, `Keep-Alive ${fmtNumber(network.http_max_keepalive || 0)} 个 · 与上游请求共用`],
    ["数据库连接池", `${fmtNumber(network.db_pool_size || 0)} 个`, `溢出 ${fmtNumber(network.db_max_overflow || 0)} · 超时 ${network.db_pool_timeout_seconds || 0}s`],
    ["图片下载与落盘", `并发 ${fmtNumber(persistence.image_download_concurrency || 0)} 个 · 单次 ${formatSeconds(persistence.image_download_timeout_seconds)}`, `总预算 ${formatSeconds(persistence.image_save_total_timeout_seconds)} · 重试 ${fmtNumber(persistence.image_download_attempts || 0)} 次 · 退避 ${formatSeconds(persistence.image_retry_backoff_seconds)}`],
    ["图片自动清理", `检查间隔 ${formatSeconds(persistence.upload_cleanup_interval_seconds)}`, "生成图与参考图按各自保留期执行；0 表示永久保留"],
    ["进程与响应", workerLabel, "进程内并发闸门要求单 worker · 本地图片落盘完成后才返回"],
  ];
  panel.innerHTML = rows
    .map(([label, value, hint]) => `
      <div class="runtime-config-item">
        <span>${escapeHtml(label)}</span>
        <strong class="mono">${escapeHtml(value)}</strong>
        <small>${escapeHtml(hint)}</small>
      </div>
    `)
    .join("");
  if (meta) {
    meta.textContent = process.single_worker_ok === false ? "部署配置需修正" : "当前进程生效值 · 只读";
  }
}

async function refreshSettings() {
  try {
    const data = await api("/api/admin/settings");
    state.runtimeConfig = data.runtime_config || null;
    renderSettingsForm(data.items);
    renderRuntimeConfig(data.runtime_config);
    renderStorageStats(data.storage);
    return true;
  } catch (err) {
    state.runtimeConfig = null;
    RETENTION_FIELDS.forEach((field) => {
      const meta = $(`#${field.metaId}`);
      if (meta) meta.textContent = `加载失败：${err.message}`;
    });
    renderRuntimeConfig(null);
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
    state.runtimeConfig = data.runtime_config || null;
    renderSettingsForm(data.items);
    renderRuntimeConfig(data.runtime_config);
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
    state.runtimeConfig = data.runtime_config || null;
    renderSettingsForm(data.items);
    renderRuntimeConfig(data.runtime_config);
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
    runStorageCleanup(["generated_images"], event.currentTarget, "过期生成图片")
  );
  $("#cleanupReferenceBtn")?.addEventListener("click", (event) =>
    runStorageCleanup(["reference_images"], event.currentTarget, "过期参考图")
  );
  RETENTION_FIELDS.forEach((field) => {
    const resetBtn = $(`#${field.resetBtnId}`);
    if (resetBtn) resetBtn.addEventListener("click", () => resetRetentionField(field));
  });
}
