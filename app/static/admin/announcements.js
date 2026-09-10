/* Admin console: announcement management. */
function resetAnnouncementForm() {
  state.announcementEditingId = null;
  $("#announcementTitleInput").value = "";
  $("#announcementContentInput").value = "";
  $("#announcementPublishedInput").checked = true;
  $("#announcementSaveBtn").textContent = "发布公告";
  $("#announcementCancelBtn").classList.add("is-hidden");
}

function renderAdminAnnouncements() {
  const list = $("#adminAnnouncementList");
  if (!list) return;
  if (!state.announcements.length) {
    list.innerHTML = '<p class="muted">暂无公告。</p>';
    return;
  }
  list.innerHTML = state.announcements.map((item) => `
    <article class="announcement-admin-item">
      <div><strong>${escapeHtml(item.title)}</strong><span class="muted">${item.published ? `已发布 · ${escapeHtml(fmtDate(item.published_at))}` : "草稿"}</span></div>
      <p>${escapeHtml(item.content)}</p>
      <div class="panel-actions"><button class="btn btn-ghost" data-announcement-edit="${escapeHtml(item.id)}" type="button">编辑</button><button class="btn btn-danger" data-announcement-delete="${escapeHtml(item.id)}" type="button">删除</button></div>
    </article>`).join("");
  list.querySelectorAll("[data-announcement-edit]").forEach((button) => button.addEventListener("click", () => {
    const item = state.announcements.find((entry) => entry.id === button.dataset.announcementEdit);
    if (!item) return;
    state.announcementEditingId = item.id;
    $("#announcementTitleInput").value = item.title;
    $("#announcementContentInput").value = item.content;
    $("#announcementPublishedInput").checked = item.published;
    $("#announcementSaveBtn").textContent = "保存公告";
    $("#announcementCancelBtn").classList.remove("is-hidden");
  }));
  list.querySelectorAll("[data-announcement-delete]").forEach((button) => button.addEventListener("click", async () => {
    if (!(await confirmAction("确认永久删除这条公告？", { title: "删除公告", confirmText: "确认删除" }))) return;
    try {
      await api(`/api/admin/announcements/${button.dataset.announcementDelete}`, { method: "DELETE" });
      await refreshAdminAnnouncements();
      showToast("公告已删除", "success");
    } catch (err) {
      showToast(`公告删除失败：${err.message}`, "error");
    }
  }));
}

async function refreshAdminAnnouncements() {
  const data = await api("/api/admin/announcements");
  state.announcements = Array.isArray(data.items) ? data.items : [];
  renderAdminAnnouncements();
  return true;
}

async function saveAnnouncement() {
  const title = $("#announcementTitleInput").value.trim();
  const content = $("#announcementContentInput").value.trim();
  if (!title || !content) return showToast("请填写公告标题和内容", "warning");
  const id = state.announcementEditingId;
  await api(id ? `/api/admin/announcements/${id}` : "/api/admin/announcements", {
    method: id ? "PUT" : "POST",
    body: JSON.stringify({ title, content, published: $("#announcementPublishedInput").checked }),
  });
  resetAnnouncementForm();
  await refreshAdminAnnouncements();
  showToast(id ? "公告已更新" : "公告已创建", "success");
}


function bindAnnouncementsPage() {
  $("#reloadAnnouncementsBtn")?.addEventListener("click", () => refreshAdminAnnouncements().catch((err) => showToast(`公告加载失败：${err.message}`, "error")));
  $("#announcementSaveBtn")?.addEventListener("click", () => saveAnnouncement().catch((err) => showToast(`公告保存失败：${err.message}`, "error")));
  $("#announcementCancelBtn")?.addEventListener("click", resetAnnouncementForm);
}
