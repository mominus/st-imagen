/* Admin console: Resources. Loaded in dependency order by admin.html. */
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
  if (state.filters.logs.category !== "all") {
    chips.push(filterChipHtml("失败类型", FAILURE_CATEGORY_LABELS[state.filters.logs.category] || state.filters.logs.category));
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
            ${account.isolation_seconds ? badgeHtml(`故障隔离 ${Math.ceil(account.isolation_seconds)}s`, "warning") : ""}
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
        <div class="table-actions">
          <button class="${toggle.className}" data-action="toggle-account" type="button">${toggle.label}</button>
          <details class="table-more">
            <summary aria-haspopup="menu">更多</summary>
            <div class="table-more-menu">
              <button class="btn btn-ghost" data-action="test" type="button">测试</button>
              <button class="btn btn-ghost" data-action="edit" type="button">编辑</button>
              ${account.isolation_seconds ? `<button class="btn btn-ghost" data-action="clear-isolation" type="button">解除隔离</button>` : ""}
              <button class="btn btn-danger" data-action="delete" type="button">删除</button>
            </div>
          </details>
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

    const clearIsolationBtn = row.querySelector('[data-action="clear-isolation"]');
    if (clearIsolationBtn) {
      clearIsolationBtn.addEventListener("click", async (event) => {
        try {
          await withBusyButton(event.currentTarget, "清除中…", async () =>
            api(`/api/admin/accounts/${encodeURIComponent(id)}/isolation/clear`, { method: "POST" })
          );
          await Promise.all([refreshAccounts(), refreshRuntimeStatus()]);
        showToast("账号故障隔离已解除", "success");
        } catch (err) {
          showToast(`清除失败：${err.message}`, "error");
        }
      });
    }
  });
}

function bindMoreMenus() {
  $$(".table-more").forEach((details) => {
    const summary = details.querySelector("summary");
    if (!summary) return;
    const syncExpandedState = () => {
      summary.setAttribute("aria-expanded", details.open ? "true" : "false");
    };
    details.addEventListener("toggle", syncExpandedState);
    details.querySelectorAll(".table-more-menu button").forEach((button) => {
      button.addEventListener("click", () => {
        details.open = false;
      });
    });
    syncExpandedState();
  });
}

function closeTableMoreMenus(except = null) {
  $$(".table-more[open]").forEach((details) => {
    if (details !== except) details.open = false;
  });
}

function renderAccountsTable() {
  const tbody = $("#accountsTable tbody");
  const summary = $("#accountsSummary");
  const items = filteredAccounts();
  const active = state.accounts.filter((item) => item.status === "active").length;
  const activeItems = state.accounts.filter((item) => item.status === "active");
  const inflight = sumBy(activeItems, (item) => item.in_flight);
  const capacity = sumBy(activeItems, (item) => item.max_inflight);
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
    const isolated = state.accounts.filter((item) => item.isolation_seconds).length;
    summary.textContent = `共 ${fmtNumber(state.accounts.length)} 个账号，${fmtNumber(active)} 个启用；当前并发 ${fmtNumber(inflight)} / ${fmtNumber(capacity)}${isolated ? `；${fmtNumber(isolated)} 个故障隔离中` : ""}。`;
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
  bindMoreMenus();
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
      <td class="col-user-failures" data-label="失败次数">
        <div class="stack-cell"><strong class="mono">${fmtNumber(user.failure_count || 0)}</strong>
        ${user.disabled_until ? `<span class="table-note table-note-warning">禁用至 ${escapeHtml(fmtDate(user.disabled_until))}</span>` : ""}</div>
      </td>
      <td class="col-user-total" data-label="累计 / 最近登录">
        <div class="entity-cell">
          <strong class="mono">${fmtNumber(user.total_requests || 0)}</strong>
          <span class="entity-meta">${escapeHtml(user.last_used_at ? `最近生成 ${fmtRelativeTime(user.last_used_at)}` : "尚无生成记录")}</span>
        </div>
      </td>
      <td class="col-user-actions" data-label="操作">
        <div class="table-actions">
          <button class="${toggle.className}" data-action="toggle-user" type="button">${toggle.label}</button>
          <details class="table-more">
            <summary aria-haspopup="menu">更多</summary>
            <div class="table-more-menu">
              <button class="btn btn-ghost" data-action="edit-user" type="button">编辑</button>
              <button class="btn btn-danger" data-action="delete-user" type="button">删除</button>
            </div>
          </details>
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
    tbody.innerHTML = renderEmptyRow(7, "暂无用户。", "可手动创建用户，或先发放邀请码。");
    return;
  }
  if (!items.length) {
    tbody.innerHTML = renderEmptyRow(7, "没有匹配结果。", "尝试放宽筛选条件。");
    return;
  }
  tbody.innerHTML = items.map(renderUserRow).join("");
  bindUserActions(items);
  bindMoreMenus();
}

async function refreshUsers() {
  const tbody = $("#usersTable tbody");
  tbody.innerHTML = renderEmptyRow(7, "用户列表加载中…", "正在同步用户数据。");
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
    tbody.innerHTML = renderErrorRow(7, err.message);
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
  const deleteButton = $("#deleteAllInvitesBtn");
  const items = filteredInvites();
  const hasFilters = state.filters.invites.query.trim() || state.filters.invites.status !== "all";
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
  if (deleteButton) {
    deleteButton.textContent = hasFilters ? `删除筛选结果${items.length ? `（${fmtNumber(items.length)}）` : ""}` : "全部删除";
    deleteButton.disabled = items.length === 0;
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
  const targets = filteredInvites();
  if (!targets.length) {
    showToast("当前筛选结果没有邀请码可删除", "info");
    return;
  }
  const deletingFiltered = targets.length !== state.invites.length;
  const prompt = deletingFiltered
    ? `确认删除当前筛选的 ${targets.length} 个邀请码？已注册用户不会被删除，但邀请码记录不可恢复。`
    : "确认删除所有邀请码？已注册用户不会被删除，但邀请码记录不可恢复。";
  if (!confirm(prompt)) return;
  try {
    await withBusyButton(button, "删除中…", async () => {
      if (!deletingFiltered) {
        try {
          await api("/api/admin/invite-codes", { method: "DELETE" });
        } catch (err) {
          if (err.status !== 404 && err.status !== 405) throw err;
          if (isMissingDeleteRouteError(err)) {
            throw new Error("当前后端实例还没加载邀请码批量删除接口，请先重启 st-imagen 服务。");
          }
          const failures = [];
          for (const invite of targets) {
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
      } else {
        const failures = [];
        for (const invite of targets) {
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
    showToast(deletingFiltered ? "筛选的邀请码已删除" : "全部邀请码已删除", "success");
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
    const matchesCategory = state.filters.logs.category === "all" || log.failure_category === state.filters.logs.category;
    return matchesQuery && matchesStatus && matchesMode && matchesCategory;
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
          ${log.failure_category ? `<span class="entity-meta">${escapeHtml(FAILURE_CATEGORY_LABELS[log.failure_category] || log.failure_category)}</span>` : ""}
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
    const data = await api("/api/admin/logs?limit=200");
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
