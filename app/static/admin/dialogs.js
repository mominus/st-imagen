/* Admin console: Dialogs. Loaded in dependency order by admin.html. */
function openAccountModal(account = null) {
  state.editing.accountId = account ? account.id : null;
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
  $("#u_daily_quota").value = String(user?.daily_quota ?? 10);
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
    daily_quota: Number($("#u_daily_quota").value || 10),
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
    daily_quota: Number($("#i_daily_quota").value || 10),
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
  $("#logFailureCategoryFilter").addEventListener("change", (event) => {
    state.filters.logs.category = event.currentTarget.value;
    if (event.currentTarget.value !== "all") state.filters.logs.status = "error";
    renderLogsTable();
  });
}

