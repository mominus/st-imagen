const { test, expect } = require("@playwright/test");

const username = process.env.E2E_ADMIN_USERNAME;
const password = process.env.E2E_ADMIN_PASSWORD;

test.skip(!username || !password, "Set E2E_ADMIN_USERNAME and E2E_ADMIN_PASSWORD");

test("mobile feedback avoids navigation and keeps modal actions reachable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/admin");
  await page.getByLabel("用户名").fill(username);
  await page.getByLabel("密码").fill(password);
  await page.getByRole("button", { name: "登录" }).click();

  const dock = page.locator(".admin-mobile-nav");
  const toast = page.locator(".toast").last();
  await expect(dock).toBeVisible();
  await expect(toast).toBeVisible();
  const dockBox = await dock.boundingBox();
  const toastBox = await toast.boundingBox();
  expect(toastBox.y + toastBox.height).toBeLessThanOrEqual(dockBox.y);

  await dock.getByText("账号", { exact: true }).click();
  await page.getByRole("button", { name: "新增账号" }).click();
  const modal = page.locator("#accountModal .modal");
  await expect(modal).toBeVisible();
  const modalBox = await modal.boundingBox();
  expect(modalBox.y).toBeGreaterThanOrEqual(0);
  expect(modalBox.y + modalBox.height).toBeLessThanOrEqual(844);
  await expect(page.locator("#accountModalSave")).toBeInViewport();

  await page.locator("#accountModalCancel").click();
  await page.setViewportSize({ width: 320, height: 720 });
  await dock.getByText("概览", { exact: true }).click();
  await expect(page.locator(".mobile-scroll-hint")).toBeVisible();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)
  ).toBeTruthy();
});
