/* Admin console: Bootstrap. Loaded in dependency order by admin.html. */
document.addEventListener("DOMContentLoaded", () => {
  bindAdminThemeToggle();

  bindFilters();
  initConsoleNav();
  bindAdminNavigation();
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
    if (event.key === "Escape" && $$(".modal-mask.show").length) {
      closeAllModals();
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
  $("#overviewRefreshBtn")?.addEventListener("click", refreshAll);
  $$("[data-dashboard-period]").forEach((button) => {
    button.addEventListener("click", () => {
      const period = button.dataset.dashboardPeriod;
      if (!period || period === state.dashboardPeriod && state.dashboardAnalytics) return;
      refreshDashboardAnalytics(period);
    });
  });

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
  $("#i_generation_mode").addEventListener("change", syncInviteGenerationMode);
  $("#inviteModalSave").addEventListener("click", saveInviteBatch);
  $("#inviteCopyBtn").addEventListener("click", copyInviteResults);
  $("#inviteModal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeInviteModal();
  });
});
