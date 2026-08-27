const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "tests/e2e",
  use: { baseURL: process.env.E2E_BASE_URL || "http://127.0.0.1:8001" },
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list"
});
