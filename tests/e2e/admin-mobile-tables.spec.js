const { test, expect } = require("@playwright/test");

// 无需真实凭据的移动端回归:预置 admin token 并拦截全部 /api 接口,
// 在 390×844 与 320×720 两档手机视口下验证四个表格页的卡片布局。

const MINUTES = 60000;
const NOW = Date.now();

const ACCOUNTS = [
  {
    id: "acc-1",
    name: "svc-alpha@example.com",
    status: "active",
    private_api_key_set: true,
    org_id: "org-9f2b1c44",
    flow_id: "flow-image-01",
    in_flight: 2,
    max_inflight: 4,
    isolation_seconds: 0,
    total_requests: 1284,
    last_used_at: new Date(NOW - 5 * MINUTES).toISOString(),
  },
  {
    id: "acc-2",
    name: "svc-beta@example.com",
    status: "disabled",
    private_api_key_set: false,
    org_id: "org-3ac77e10",
    flow_id: "flow-image-02",
    in_flight: 0,
    max_inflight: 4,
    isolation_seconds: 42,
    total_requests: 87,
    last_used_at: null,
  },
];

const USERS = [
  {
    id: "u-1",
    username: "alice",
    status: "active",
    expires_at: new Date(NOW + 3 * 86400000).toISOString(),
    daily_used: 3,
    daily_quota: 50,
    in_flight: 0,
    max_inflight: 2,
    total_requests: 128,
    failure_count: 1,
    disabled_until: null,
    last_login_at: new Date(NOW - 120 * MINUTES).toISOString(),
    last_used_at: new Date(NOW - 5 * MINUTES).toISOString(),
    invite_code_id: "iv-1",
  },
  {
    id: "u-2",
    username: "bob",
    status: "active",
    expires_at: null,
    daily_used: 12,
    daily_quota: 20,
    in_flight: 1,
    max_inflight: 2,
    total_requests: 77,
    failure_count: 0,
    disabled_until: null,
    last_login_at: null,
    last_used_at: null,
    invite_code_id: null,
  },
];

const INVITES = [
  {
    id: "iv-1",
    code_prefix: "AB12",
    code_suffix: "9f2b1c88",
    raw_code: "",
    status: "active",
    used_count: 6,
    max_uses: 10,
    daily_quota: 20,
    max_inflight: 2,
    expires_at: new Date(NOW + 10 * 86400000).toISOString(),
    note: "给新用户注册使用,包含每日 20 次生成额度,并发上限 2。",
  },
  {
    id: "iv-2",
    code_prefix: "CD34",
    code_suffix: "5c6d7e90",
    raw_code: "",
    status: "revoked",
    used_count: 10,
    max_uses: 10,
    daily_quota: 50,
    max_inflight: 3,
    expires_at: null,
    note: "",
  },
];

const LOGS = [
  {
    id: "lg-1",
    timestamp: new Date(NOW - 2 * MINUTES).toISOString(),
    mode: "text2img",
    is_stream: true,
    model: "gpt-image-2",
    aspect_ratio: "1:1",
    resolution: "1K",
    username: "alice",
    account_name: "svc-alpha@example.com",
    response_time_ms: 8421,
    status: "success",
    failure_category: null,
    error_message: "",
    output_images: ["https://img.example.com/1.png"],
    output_preview: null,
    prompt_preview: "一只在月光下的雪山垭口奔跑的赤狐,电影感构图,35mm 胶片颗粒,超广角;".repeat(24),
  },
  {
    id: "lg-2",
    timestamp: new Date(NOW - 30 * MINUTES).toISOString(),
    mode: "img2img",
    is_stream: false,
    model: "seedream-4",
    aspect_ratio: "3:4",
    resolution: "2K",
    username: "bob",
    account_name: "svc-beta@example.com",
    response_time_ms: 3120,
    status: "error",
    failure_category: "upstream",
    error_message:
      "上游服务暂时不可用,该请求未计入用户额度;账号已自动隔离观察 42 秒,请稍后重试,若持续失败请联系管理员检查账号池状态。".repeat(
        6,
      ),
    output_images: [],
    output_preview: null,
    prompt_preview: "把背景换成雪山",
  },
];

async function stubAdminApi(page) {
  // 兜底最先注册(Playwright 后注册的优先),其余未列举接口一律返回空对象。
  await page.route("**/api/**", (route) => route.fulfill({ json: {} }));
  await page.route("**/api/admin/me", (route) => route.fulfill({ json: { username: "admin" } }));
  await page.route("**/api/admin/accounts", (route) => route.fulfill({ json: { items: ACCOUNTS } }));
  await page.route("**/api/admin/users", (route) => route.fulfill({ json: { items: USERS } }));
  await page.route("**/api/admin/invite-codes", (route) => route.fulfill({ json: { items: INVITES } }));
  await page.route("**/api/admin/logs**", (route) => route.fulfill({ json: { items: LOGS } }));
  await page.route("**/api/admin/stats/overview", (route) => route.fulfill({ json: {} }));
  // 让驾驶舱聚合快照失败,走逐接口兜底路径,四张表各自渲染。
  await page.route("**/api/admin/dashboard/snapshot**", (route) =>
    route.fulfill({ status: 500, json: { detail: "stub" } }),
  );
  await page.route("**/api/admin/stats/dashboard**", (route) => route.fulfill({ json: {} }));
  await page.route("**/api/admin/settings", (route) => route.fulfill({ json: {} }));
}

async function expectNoHorizontalOverflow(page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
}

for (const [width, height] of [
  [390, 844],
  [320, 720],
]) {
  test(`admin tables become full-detail cards at ${width}x${height}`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.addInitScript(() => {
      localStorage.setItem("image_gen_admin_token", "e2e-stub-token");
    });
    await stubAdminApi(page);

    // --- 账号 ---
    await page.goto("/admin/accounts");
    await expect(page.locator("#accountsTable td.col-account-name").first()).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await expect(page.locator("#accountsTable .capacity-bar").first()).toBeVisible();
    await expect(page.locator("#accountsTable td.col-account-org-flow").first()).toBeVisible();
    await expect(page.locator("#accountsTable td.col-account-total").first()).toBeVisible();
    await expect(
      page.locator('#accountsTable button[data-action="toggle-account"]').first(),
    ).toBeVisible();
    await page.locator("#accountsTable .table-more summary").first().click();
    await expect(page.locator("#accountsTable .table-more-menu .btn").first()).toBeVisible();
    await page.locator("#accountsTable .table-more summary").first().click();

    // --- 用户 ---
    await page.goto("/admin/users");
    await expect(page.locator("#usersTable td.col-user-name").first()).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await expect(page.locator("#usersTable td.col-user-expiry").first()).toBeVisible();
    await expect(page.locator("#usersTable td.col-user-failures").first()).toBeVisible();
    await expect(page.locator("#usersTable td.col-user-quota strong").first()).toBeVisible();
    await expect(
      page.locator('#usersTable button[data-action="toggle-user"]').first(),
    ).toBeVisible();

    // --- 邀请码 ---
    await page.goto("/admin/invites");
    await expect(page.locator("#invitesTable td.col-invite-code").first()).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await expect(page.locator("#invitesTable .capacity-bar").first()).toBeVisible();
    await expect(page.locator("#invitesTable td.col-invite-note").first()).toBeVisible();
    await expect(
      page.locator('#invitesTable button[data-action="revoke-invite"]').first(),
    ).toBeVisible();

    // --- 日志 ---
    await page.goto("/admin/logs");
    await expect(page.locator("#logsTable td.col-log-time").first()).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await expect(page.locator("#logsTable td.col-log-account").first()).toBeVisible();
    await expect(page.locator("#logsTable td.col-log-duration").first()).toBeVisible();
    await expect(page.locator("#logsTable td.col-log-status").first()).toBeVisible();
    await expect(page.locator('#logsTable button[data-action="view-log"]').first()).toBeVisible();

    // --- 创作详情弹窗 ---
    const modalBody = page.locator("#previewModal .preview-modal");

    // 成功日志 + 超长提示词:提示词滚动盒必须完整落在弹窗内,不得溢出面板
    await page.locator('#logsTable button[data-action="view-log"]').first().click();
    await expect(page.locator("#previewModal .preview-prompt")).toBeVisible();
    const modalBox = await modalBody.boundingBox();
    const promptBox = await page.locator("#previewModal .preview-prompt-scroll").boundingBox();
    expect(promptBox.y).toBeGreaterThanOrEqual(modalBox.y);
    expect(promptBox.y + promptBox.height).toBeLessThanOrEqual(modalBox.y + modalBox.height + 1);
    const panelBox = await page
      .locator("#previewModal .preview-modal-admin .preview-panel")
      .first()
      .boundingBox();
    expect(promptBox.y + promptBox.height).toBeLessThanOrEqual(panelBox.y + panelBox.height + 1);
    await expect(page.locator("#previewErrorBlock")).toBeHidden();
    await page.keyboard.press("Escape");
    await expect(modalBody).toBeHidden();

    // 失败日志:报错详情占据图片位置展示,侧栏报错块隐藏
    await page.locator('#logsTable button[data-action="view-log"]').nth(1).click();
    const stageFailure = page.locator("#previewEmptyState.preview-stage-empty-danger");
    await expect(stageFailure).toBeVisible();
    await expect(page.locator("#previewEmptyState .preview-stage-empty-title")).toHaveText(
      "生成失败",
    );
    await expect(page.locator("#previewEmptyState .preview-stage-empty-text")).toContainText(
      "上游服务暂时不可用",
    );
    await expect(page.locator("#previewErrorBlock")).toBeHidden();
    const failureBox = await stageFailure.boundingBox();
    expect(failureBox.y).toBeGreaterThanOrEqual(modalBox.y);
    expect(failureBox.y + failureBox.height).toBeLessThanOrEqual(modalBox.y + modalBox.height + 1);
    await page.keyboard.press("Escape");
    await expect(modalBody).toBeHidden();

    // 关闭后状态复位:再次打开成功日志,舞台不再残留失败样式
    await page.locator('#logsTable button[data-action="view-log"]').first().click();
    await expect(page.locator("#previewModal .preview-prompt")).toBeVisible();
    await expect(page.locator("#previewEmptyState")).toBeHidden();
    await page.keyboard.press("Escape");
  });
}

test("mobile dock gains an invites entry that opens the invites page", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => {
    localStorage.setItem("image_gen_admin_token", "e2e-stub-token");
  });
  await stubAdminApi(page);

  await page.goto("/admin/accounts");
  const dock = page.locator(".admin-mobile-nav");
  await expect(dock).toBeVisible();
  await expect(dock.locator("a")).toHaveCount(6);

  await dock.getByText("邀请码", { exact: true }).click();
  await expect(page.locator("#invitesPage")).toBeVisible();
  await expect(page.locator("#invitesTable td.col-invite-code").first()).toBeVisible();
});

test("mobile feedback layers stay clear of the dock and modals stay reachable", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => {
    localStorage.setItem("image_gen_admin_token", "e2e-stub-token");
  });
  await stubAdminApi(page);

  await page.goto("/admin/accounts");
  const dock = page.locator(".admin-mobile-nav");
  await expect(dock).toBeVisible();

  // toast 不得压住底部导航(镜像 admin-mobile.spec.js 的几何断言)
  await page.waitForTimeout(400);
  const toast = page.locator(".toast").last();
  if ((await toast.count()) > 0) {
    await expect(toast).toBeVisible();
    const toastBox = await toast.boundingBox();
    const dockBox = await dock.boundingBox();
    expect(toastBox.y + toastBox.height).toBeLessThanOrEqual(dockBox.y);
  }

  // 新增账号弹窗完整落在视口内,保存按钮可达
  await page.getByRole("button", { name: "新增账号" }).click();
  const modal = page.locator("#accountModal .modal");
  await expect(modal).toBeVisible();
  const modalBox = await modal.boundingBox();
  expect(modalBox.y).toBeGreaterThanOrEqual(0);
  expect(modalBox.y + modalBox.height).toBeLessThanOrEqual(844);
  await expect(page.locator("#accountModalSave")).toBeInViewport();
});
