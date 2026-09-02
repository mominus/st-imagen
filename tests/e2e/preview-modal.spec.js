const { test, expect } = require("@playwright/test");

const TINY_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
  "base64",
);

function recentItem(id, url, prompt) {
  return {
    id,
    generation_id: id.split(":")[0],
    timestamp: "2026-09-01T10:00:00+08:00",
    image_url: url,
    prompt_preview: prompt,
    mode: "text2img",
    model: "test-model",
    aspect_ratio: "1:1",
    resolution: "1K",
    response_time_ms: 100,
  };
}

test.beforeEach(async ({ page }) => {
  // 兜底：其余 /api 请求一律返回空对象，测试自身只依赖下面几个精确 stub
  await page.route("**/api/**", (route) => route.fulfill({ json: {} }));
  await page.route("**/api/auth/status", (route) =>
    route.fulfill({
      json: { linuxdo_enabled: false, user: { username: "tester", display_name: "Tester" } },
    }),
  );
  await page.route("**/api/recent-images*", (route) =>
    route.fulfill({
      json: {
        items: [
          recentItem("1:0", "/stub/missing.png", "已被清理的图片"),
          recentItem(
            "2:0",
            "/stub/present.png",
            "一只在月光下的雪山垭口奔跑的赤狐,电影感构图,35mm 胶片颗粒,超广角,清晨蓝色时刻,冷暖对比,毛发细节,尘埃与光斑;".repeat(
              16,
            ),
          ),
        ],
        total: 2,
      },
    }),
  );
  await page.route("**/stub/missing.png", (route) => route.fulfill({ status: 404, body: "gone" }));
  await page.route("**/stub/present.png", (route) =>
    route.fulfill({ status: 200, contentType: "image/png", body: TINY_PNG }),
  );
});

test("preview recovers when switching from a cleaned image to a loadable one", async ({ page }) => {
  await page.goto("/");
  const cards = page.locator(".gallery-card");
  await expect(cards).toHaveCount(2);

  // 先打开一张已被清理的图片：显示"图片已被清理或丢失"
  await cards.nth(0).click();
  await expect(page.locator("#previewEmptyState")).toBeVisible();
  await expect(page.locator("#previewImage")).toBeHidden();

  // 切换到下一张（可正常加载）的新图：图片必须可见，空状态必须隐藏
  await page.locator("#previewNextBtn").click();
  await expect(page.locator("#previewImage")).toBeVisible();
  await expect(page.locator("#previewEmptyState")).toBeHidden();

  // 再切回被清理的图，然后再次切回新图：新图仍然可见
  await page.locator("#previewPrevBtn").click();
  await expect(page.locator("#previewEmptyState")).toBeVisible();
  await page.locator("#previewNextBtn").click();
  await expect(page.locator("#previewImage")).toBeVisible();
  await expect(page.locator("#previewEmptyState")).toBeHidden();
});

test("preview is visible when opening a loadable image after closing a failed preview", async ({
  page,
}) => {
  await page.goto("/");
  const cards = page.locator(".gallery-card");
  await expect(cards).toHaveCount(2);

  await cards.nth(0).click();
  await expect(page.locator("#previewEmptyState")).toBeVisible();
  await page.locator("#previewModalClose").click();

  // 关闭失败预览后直接点开新图：图片必须可见
  await cards.nth(1).click();
  await expect(page.locator("#previewImage")).toBeVisible();
  await expect(page.locator("#previewEmptyState")).toBeHidden();
});

test("creation detail keeps prompt, copy button and footer separated on phones", async ({
  page,
}) => {
  for (const [width, height] of [
    [390, 844],
    [320, 720],
  ]) {
    await page.setViewportSize({ width, height });
    await page.goto("/");
    const cards = page.locator(".gallery-card");
    await expect(cards).toHaveCount(2);

    await cards.nth(1).click();
    const modal = page.locator("#previewModal");
    await expect(modal).toHaveClass(/show/);

    // 复制按钮与响应时间页脚必须可见且落在视口内
    const copyBtn = page.locator("#previewCopyBtn");
    await expect(copyBtn).toBeVisible();
    await expect(copyBtn).toBeInViewport();
    const response = page.locator("#previewResponseTime");
    await expect(response).toBeVisible();
    await expect(response).toBeInViewport();

    // 几何约束:提示词面板不压页脚,各盒子都不超出弹窗
    const modalBody = page.locator("#previewModal .preview-modal");
    const panel = page.locator("#previewModal .preview-panel");
    const footer = page.locator("#previewModal .preview-footer");
    const panelBox = await panel.boundingBox();
    const footerBox = await footer.boundingBox();
    const modalBox = await modalBody.boundingBox();
    expect(panelBox.y + panelBox.height).toBeLessThanOrEqual(footerBox.y + 1);
    expect(footerBox.y + footerBox.height).toBeLessThanOrEqual(modalBox.y + modalBox.height + 1);

    // 超长提示词在滚动盒内部滚动,滚动盒本身不超出面板
    const scroll = page.locator("#previewModal .preview-prompt-scroll");
    const scrollBox = await scroll.boundingBox();
    expect(scrollBox.y + scrollBox.height).toBeLessThanOrEqual(panelBox.y + panelBox.height + 1);
    const scrollInfo = await scroll.evaluate((el) => ({
      scroll: el.scrollHeight,
      client: el.clientHeight,
    }));
    expect(scrollInfo.scroll).toBeGreaterThan(scrollInfo.client);

    await page.keyboard.press("Escape");
    await expect(modal).not.toHaveClass(/show/);
  }
});
