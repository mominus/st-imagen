const { test, expect } = require("@playwright/test");

// 手机端首页登录/注册面板:表单必须完整可达,弹窗可滚动兜底。
// 全部走真实静态资源;接口不做登录态 stub(匿名即可打开登录面板),
// 因此对真实后端与静态服务器同样有效。

for (const [width, height] of [
  [390, 844],
  [320, 720],
]) {
  test(`auth panel stays reachable on phones at ${width}x${height}`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.goto("/");
    await expect(page.locator("#authEntryBtn")).toBeVisible();
    await page.locator("#authEntryBtn").click();

    const modal = page.locator("#authModal .modal");
    await expect(modal).toBeVisible();

    // 弹窗不得超出视口
    const modalBox = await modal.boundingBox();
    expect(modalBox.y).toBeGreaterThanOrEqual(0);
    expect(modalBox.y + modalBox.height).toBeLessThanOrEqual(height + 1);

    // 登录表单完整可见
    await expect(page.locator("#authLoginUsername")).toBeVisible();
    await expect(page.locator("#authLoginBtn")).toBeVisible();
    await expect(page.locator("#authLoginBtn")).toBeInViewport();

    // 切换到注册 pane,邀请码输入与提交按钮同样可达
    await page.locator('[data-auth-mode="activate"]').click();
    await expect(page.locator("#authInviteCode")).toBeVisible();
    await expect(page.locator("#authActivateBtn")).toBeVisible();
    await expect(page.locator("#authActivateBtn")).toBeInViewport();

    // 弹窗内容过高时可滚动到底部(不要求发生,只要求能力存在)
    await page.locator("#authTabs").scrollIntoViewIfNeeded();
    const scrollable = await modal.evaluate((el) => el.scrollHeight - el.clientHeight);
    if (scrollable > 0) {
      await modal.evaluate((el) => el.scrollTo(0, el.scrollHeight));
      const atBottom = await modal.evaluate(
        (el) => el.scrollTop + el.clientHeight >= el.scrollHeight - 2,
      );
      expect(atBottom).toBeTruthy();
    }
  });
}
