import { createHash } from "node:crypto";

// 固定测试身份只用于 Test Flow 合成会话；真实网站仍由 authenticate 提供身份。
export const WEBSITE_TEST_NAMESPACE = "xiaodao-test-flow";
export const WEBSITE_TEST_USER = "cross-job-browser";
export const WEBSITE_TEST_OWNER = createHash("sha256")
  .update(JSON.stringify([WEBSITE_TEST_NAMESPACE, WEBSITE_TEST_USER])).digest("hex");
