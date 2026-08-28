const { test, expect } = require("@playwright/test");

test("home page and public options are available", async ({ page, request }) => {
  await page.goto("/");
  await expect(page).toHaveTitle(/ST|Imagen|图像/i);
  const response = await request.get("/api/options");
  expect(response.ok()).toBeTruthy();
  expect(await response.json()).toHaveProperty("text2img");
});
