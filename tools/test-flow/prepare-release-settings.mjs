#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

import {
  CLAUDE_SETTINGS_ENV_KEYS,
  RELEASE_MODEL,
  claudeSettingsIdentity,
} from "./lib/release-inputs.mjs";
import { canonicalJson, ensureDirectory } from "./lib/util.mjs";

function argument(name) {
  const index = process.argv.indexOf(name);
  return index === -1 ? null : process.argv[index + 1];
}

function main() {
  const output = argument("--output");
  if (!output || !path.isAbsolute(output)) throw new Error("SETTINGS_OUTPUT_ABSOLUTE_REQUIRED");
  if (fs.existsSync(output)) throw new Error("SETTINGS_OUTPUT_ALREADY_EXISTS");
  const token = process.env.ANTHROPIC_AUTH_TOKEN;
  const endpoint = process.env.ANTHROPIC_BASE_URL;
  if (!token) throw new Error("ANTHROPIC_AUTH_TOKEN_REQUIRED");
  let parsedEndpoint;
  try { parsedEndpoint = new URL(endpoint); } catch { throw new Error("ANTHROPIC_BASE_URL_INVALID"); }
  if (parsedEndpoint.protocol !== "https:" || !parsedEndpoint.host || parsedEndpoint.username || parsedEndpoint.password) {
    throw new Error("ANTHROPIC_BASE_URL_HTTPS_REQUIRED");
  }
  const environment = {
    ANTHROPIC_AUTH_TOKEN: token,
    ANTHROPIC_BASE_URL: endpoint,
    ANTHROPIC_DEFAULT_HAIKU_MODEL: RELEASE_MODEL,
    ANTHROPIC_DEFAULT_OPUS_MODEL: RELEASE_MODEL,
    ANTHROPIC_DEFAULT_SONNET_MODEL: RELEASE_MODEL,
    API_TIMEOUT_MS: "600000",
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
  };
  if (canonicalJson(Object.keys(environment).sort()) !== canonicalJson([...CLAUDE_SETTINGS_ENV_KEYS].sort())) {
    throw new Error("SETTINGS_ENV_ALLOWLIST_INTERNAL_ERROR");
  }
  ensureDirectory(path.dirname(output));
  const descriptor = fs.openSync(output, "wx", 0o600);
  try {
    fs.writeFileSync(descriptor, canonicalJson({ env: environment }), "utf8");
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
  fs.chmodSync(output, 0o600);
  const identity = claudeSettingsIdentity(output);
  if (identity.status !== "PRESENT") {
    fs.rmSync(output, { force: true });
    throw new Error(`SETTINGS_VALIDATION_FAILED:${identity.code}`);
  }
  process.stdout.write(`${JSON.stringify({
    status: "PASS",
    output,
    endpoint: identity.endpoint,
    model: identity.model,
    copied_top_level_keys: identity.copied_top_level_keys,
    copied_env_key_count: identity.copied_env_key_count,
    hooks_copied: identity.hooks_copied,
  })}\n`);
}

try {
  main();
} catch (error) {
  process.stderr.write(`${String(error?.message ?? error)}\n`);
  process.exitCode = 1;
}
