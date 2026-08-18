import fs from "node:fs";
import path from "node:path";

import { resolveCommand, runSync, sha256File } from "./util.mjs";

const CHROME_IDENTITY_CACHE = new Map();


export function resolveChromeExecutable(environment = process.env, platform = process.platform) {
  const explicit = environment.TEST_FLOW_CHROME;
  if (explicit) {
    const resolved = path.isAbsolute(explicit)
      ? path.resolve(explicit)
      : resolveCommand(explicit);
    return resolved && fs.existsSync(resolved) && fs.statSync(resolved).isFile()
      ? resolved
      : null;
  }

  const candidates = platform === "win32"
    ? [
        path.join(environment.ProgramFiles ?? "C:\\Program Files", "Google", "Chrome", "Application", "chrome.exe"),
        path.join(environment["ProgramFiles(x86)"] ?? "C:\\Program Files (x86)", "Google", "Chrome", "Application", "chrome.exe"),
        environment.LOCALAPPDATA
          ? path.join(environment.LOCALAPPDATA, "Google", "Chrome", "Application", "chrome.exe")
          : null,
      ]
    : platform === "darwin"
      ? ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
      : [
          resolveCommand("google-chrome-stable"),
          resolveCommand("google-chrome"),
          "/opt/google/chrome/chrome",
        ];
  return candidates.find(
    (candidate) => candidate && fs.existsSync(candidate) && fs.statSync(candidate).isFile(),
  ) ?? null;
}


export function chromeIdentity(
  environment = process.env,
  platform = process.platform,
  invoke = runSync,
) {
  const executable = resolveChromeExecutable(environment, platform);
  if (!executable) {
    return {
      status: "MISSING",
      product: "Google Chrome",
      version: null,
      executable_sha256: null,
      code: "CHROME_REQUIRED",
    };
  }
  const stat = fs.statSync(executable);
  const cacheKey = invoke === runSync
    ? `${platform}:${executable}:${stat.size}:${stat.mtimeMs}`
    : null;
  if (cacheKey && CHROME_IDENTITY_CACHE.has(cacheKey)) {
    return { ...CHROME_IDENTITY_CACHE.get(cacheKey) };
  }
  const probe = platform === "win32"
    ? invoke("powershell.exe", [
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        `$path=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${Buffer.from(executable, "utf8").toString("base64")}')); $info=(Get-Item -LiteralPath $path).VersionInfo; [Console]::Out.Write(($info.ProductName + '|' + $info.ProductVersion))`,
      ])
    : invoke(executable, ["--version"]);
  const versionLine = `${probe.stdout ?? ""}\n${probe.stderr ?? ""}`
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => /^Google Chrome(?:\s+|\|)\d+(?:\.\d+){2,3}$/.test(line)) ?? null;
  const version = versionLine?.replace("|", " ") ?? null;
  if (probe.status !== 0 || version === null) {
    const identity = {
      status: "INVALID",
      product: "Google Chrome",
      version,
      executable_sha256: sha256File(executable),
      code: "CHROME_VERSION_INVALID",
    };
    if (cacheKey) CHROME_IDENTITY_CACHE.set(cacheKey, identity);
    return { ...identity };
  }
  const identity = {
    status: "PRESENT",
    product: "Google Chrome",
    version,
    executable_sha256: sha256File(executable),
    code: null,
  };
  if (cacheKey) CHROME_IDENTITY_CACHE.set(cacheKey, identity);
  return { ...identity };
}
