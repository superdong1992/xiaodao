import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

import { materializeClaudeSettings } from "../../../lib/release-inputs.mjs";
import { runClaudeProcess } from "../../claude-deepseek/runtime/claude-deepseek-process.mjs";
import {
  DIAGNOSIS_MAX_TURNS,
  DIAGNOSIS_LIMITS,
  DIAGNOSIS_WALL_SECONDS,
  GENERATED_SKILL_NAME,
  auditInvocationUsage,
  copyTree,
  createEmptyRoot,
  finalText,
  requireContract,
  writeJsonExclusive,
  CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
} from "./lan-skill-contract.mjs";


const JOB_ID = "00000000-0000-4000-8000-000000000011";
const CASE_ID = "00000000-0000-4000-8000-000000000001";
const ARTIFACT_ID = "00000000-0000-4000-8000-000000000060";
const PROBLEM_TIME = "2026-08-23T10:00:05.300Z";
const PROPOSAL_KEY = "lan-rpc-timeout";
const BROKER_COMMAND = `problem-locator-logparse target-logs --request output/proposals/${PROPOSAL_KEY}/request.json --result output/proposals/${PROPOSAL_KEY}/target_logs.json`;
const CLIENT_DELIVERY_NAME = "client__rpc__slot_1__rpc_client.log";
const SERVER_DELIVERY_NAME = "server__rpc__slot_2__rpc_server.log";


function writeManifest(workspaceRoot) {
  writeJsonExclusive(path.join(workspaceRoot, "inputs", "manifest.json"), {
    schema_version: 2,
    job_id: JOB_ID,
    case_id: CASE_ID,
    job_type: "DIAGNOSE",
    logparse_product: "rpc-skill-feasibility",
    logparse_tool_ref: { id: "logparse", version: "contract-stub-v1", content_hash: "f".repeat(64) },
    entries: [
      {
        input_kind: "ATTACHMENT",
        resource_id: "00000000-0000-4000-8000-000000000050",
        relative_path: "inputs/log_archive.tgz",
        content_type: "application/gzip",
        filename_suffix: ".tgz",
        resource_kind: "FILE",
        size: 21,
        sha256: "a".repeat(64),
      },
      {
        input_kind: "ARTIFACT",
        resource_id: ARTIFACT_ID,
        artifact_kind: "LOGPARSE_RUN",
        name: "lan-fast-e2e-logparse-run",
        relative_path: "inputs/artifacts/logparse-run/tree",
        content_type: "application/vnd.problem-locator.logparse-run+directory",
        resource_kind: "DIRECTORY",
        size: 1,
        sha256: "b".repeat(64),
        metadata: {
          tree_manifest_sha256: "b".repeat(64),
          logparse_version_ref: { id: "logparse", version: "contract-stub-v1", content_hash: "f".repeat(64) },
          parse_manifest_relative_path: "parse_manifest.json",
          source_attachment_id: "00000000-0000-4000-8000-000000000050",
          source_attachment_sha256: "a".repeat(64),
          parse_parameters: { product: "rpc-skill-feasibility" },
        },
      },
    ],
    resolved_logparse_plan: {
      schema_version: 1,
      problem_time: PROBLEM_TIME,
      anchors: [
        { label: "client", module: "rpc", slot: "1", process_name: "rpc_client", pid: null },
        { label: "server", module: "rpc", slot: "2", process_name: "rpc_server", pid: null },
      ],
      attachment_id: null,
      artifact_id: ARTIFACT_ID,
    },
    review_subject: null,
  });
}


function prepareWorkspace({ workspaceRoot, configRoot, generatedSkillRoot, helperSkillRoot, brokerStub, clientLog, serverLog }) {
  fs.mkdirSync(workspaceRoot, { recursive: true, mode: 0o700 });
  fs.mkdirSync(configRoot, { recursive: true, mode: 0o700 });
  const git = path.join(workspaceRoot, ".git");
  fs.mkdirSync(path.join(git, "objects"), { recursive: true, mode: 0o700 });
  fs.mkdirSync(path.join(git, "refs", "heads"), { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(git, "HEAD"), "ref: refs/heads/main\n", { mode: 0o600, flag: "wx" });
  for (const relative of ["inputs", "inputs/artifacts/logparse-run/tree", "runtime/stub-source-logs", `output/proposals/${PROPOSAL_KEY}`, "output/delivery"]) fs.mkdirSync(path.join(workspaceRoot, ...relative.split("/")), { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(workspaceRoot, "inputs", "log_archive.tgz"), "LAN_FAST_E2E_ARCHIVE\n", { mode: 0o600, flag: "wx" });
  fs.writeFileSync(path.join(workspaceRoot, "inputs", "artifacts", "logparse-run", "tree", "parse_manifest.json"), "{}\n", { mode: 0o600, flag: "wx" });
  fs.copyFileSync(clientLog, path.join(workspaceRoot, "runtime", "stub-source-logs", "client.log"), fs.constants.COPYFILE_EXCL);
  fs.copyFileSync(serverLog, path.join(workspaceRoot, "runtime", "stub-source-logs", "server.log"), fs.constants.COPYFILE_EXCL);
  writeManifest(workspaceRoot);
  copyTree(generatedSkillRoot, path.join(configRoot, "skills", GENERATED_SKILL_NAME));
  copyTree(helperSkillRoot, path.join(configRoot, "skills", "logparse-diagnose"));
  const binRoot = path.join(workspaceRoot, "runtime", "contract-bin");
  fs.mkdirSync(binRoot, { mode: 0o700 });
  const installedStub = path.join(binRoot, "problem-locator-logparse");
  fs.copyFileSync(brokerStub, installedStub, fs.constants.COPYFILE_EXCL);
  fs.chmodSync(installedStub, 0o700);
  return { binRoot };
}


export function diagnosisPrompt(scenario) {
  const common = `Use the ${GENERATED_SKILL_NAME} Skill for this RPC timeout. Your first action must call the Skill tool with exactly {"skill":"${GENERATED_SKILL_NAME}"}. The available inputs are problem_time=${PROBLEM_TIME}, client_process_name=rpc_client, server_process_name=rpc_server, service=svc_orders, api=Reserve, and log_archive=inputs/log_archive.tgz. Do not invent any missing value.`;
  if (scenario === "missing-slots") return `${common}\n\nNeither client_slot nor server_slot was provided. Follow the generated Skill's missing-input behavior. Do not load logparse-diagnose, do not call Bash, and do not create result.zip.`;
  return `${common}\n\nThe remaining inputs are client_slot=1 and server_slot=2. Follow the generated Skill completely. Load the installed logparse-diagnose Skill and obey its current contract. The manifest already contains the accepted LOGPARSE_RUN, so use target-logs rather than parsing again. The empty proposal and delivery directories are already prepared. Use proposal key ${PROPOSAL_KEY}. The broker client is already on PATH: invoke exactly \`${BROKER_COMMAND}\`; do not inspect PATH or prefix the command with a runtime directory. Analyze and Read only the returned target_logs. After reading both logs, copy them with exactly these two commands and no other copy command: \`cp output/proposals/${PROPOSAL_KEY}/target-logs/client.log output/delivery/${CLIENT_DELIVERY_NAME}\` and \`cp output/proposals/${PROPOSAL_KEY}/target-logs/server.log output/delivery/${SERVER_DELIVERY_NAME}\`. Write output/delivery/result.txt, then generate output/result.zip by invoking the generated Skill's exact pack_result_zip.py with /usr/bin/python3. Before packing, at most one read-only ls command may inspect only the loaded Skill's scripts directory and/or output/delivery. Do not run any verification or listing command after the packer succeeds. Directly return the conclusion summary, key evidence, evidence gaps, used logs, and output/result.zip path. Do not emit old Problem Locator outcome drafts.`;
}


function auditReadScope(processResult, workspaceRoot, allowedLogPaths) {
  const unexpected = [];
  const observed = new Set();
  for (const record of processResult.records) {
    if (record.name !== "Read" || typeof record.input?.file_path !== "string" || !record.input.file_path.endsWith(".log")) continue;
    const absolute = path.resolve(workspaceRoot, record.input.file_path);
    if (!allowedLogPaths.has(absolute)) unexpected.push(record.input.file_path);
    else observed.add(absolute);
  }
  requireContract(unexpected.length === 0, "LAN_DIAGNOSIS_LOG_SCOPE_INVALID", "Diagnosis read logs outside broker-returned targets", { unexpected });
  requireContract([...allowedLogPaths].every((target) => observed.has(target)), "LAN_DIAGNOSIS_REQUIRED_LOG_NOT_READ", "Diagnosis must read every broker-returned target log", { missing: [...allowedLogPaths].filter((target) => !observed.has(target)) });
}


function auditMissingSlots({ processResult, workspaceRoot }) {
  requireContract(processResult.skills.length === 1 && processResult.skills[0].skill === GENERATED_SKILL_NAME, "LAN_MISSING_SLOT_SKILL_CALL_INVALID", "Missing-slot scenario must load only the generated Skill");
  requireContract(processResult.bash.length === 0 && processResult.mcp.length === 0 && processResult.denied.length === 0, "LAN_MISSING_SLOT_TOOL_SCOPE_INVALID", "Missing-slot scenario must not call Logparse, Bash, MCP, or a denied tool");
  requireContract(!fs.existsSync(path.join(workspaceRoot, "runtime", "broker-stub-audit.json")), "LAN_MISSING_SLOT_BROKER_CALLED", "Missing-slot scenario called the broker");
  requireContract(!fs.existsSync(path.join(workspaceRoot, "output", "result.zip")), "LAN_MISSING_SLOT_ZIP_CREATED", "Missing-slot scenario created result.zip");
  const text = finalText(processResult.events);
  requireContract(text.includes("client_slot") && text.includes("server_slot"), "LAN_MISSING_SLOT_FINAL_INVALID", "Missing-slot response must request client_slot and server_slot");
  return { schema_version: 1, status: "PASS", scenario: "missing-slots", requested_inputs: ["client_slot", "server_slot"], helper_called: false, broker_called: false, zip_created: false };
}


function auditComplete({ processResult, workspaceRoot, pythonEntry, sourceClient, sourceServer }) {
  requireContract(processResult.skills.length === 2 && processResult.skills[0].skill === GENERATED_SKILL_NAME && processResult.skills[1].skill === "logparse-diagnose", "LAN_COMPLETE_SKILL_CALL_INVALID", "Complete scenario must load the generated Skill then logparse-diagnose exactly once");
  const brokerCalls = processResult.bash.filter((item) => /(?:^|\s)problem-locator-logparse\s+target-logs(?:\s|$)/u.test(item.command ?? ""));
  const copyCalls = processResult.bash.filter((item) => /^\s*cp\s+/u.test(item.command ?? ""));
  const packerCalls = processResult.bash.filter((item) => /pack_result_zip\.py(?:\s|$)/u.test(item.command ?? ""));
  const listingCalls = processResult.bash.filter((item) => /^\s*ls\s+/u.test(item.command ?? ""));
  requireContract(processResult.bash.length === 4 + listingCalls.length && listingCalls.length <= 1 && processResult.mcp.length === 0 && processResult.denied.length === 0, "LAN_COMPLETE_TOOL_SCOPE_INVALID", "Complete scenario may execute only the broker, two controlled log copies, fixed packer, and at most one scoped read-only listing");
  requireContract(brokerCalls.length === 1 && brokerCalls[0].exit_code === 0 && brokerCalls[0].command.trim() === BROKER_COMMAND, "LAN_COMPLETE_BROKER_CALL_INVALID", "Complete scenario must execute the one fixed target-logs command");
  const expectedCopyCommands = [
    `cp output/proposals/${PROPOSAL_KEY}/target-logs/client.log output/delivery/${CLIENT_DELIVERY_NAME}`,
    `cp output/proposals/${PROPOSAL_KEY}/target-logs/server.log output/delivery/${SERVER_DELIVERY_NAME}`,
  ];
  requireContract(copyCalls.length === expectedCopyCommands.length && copyCalls.every((item, index) => item.exit_code === 0 && item.command.trim() === expectedCopyCommands[index]), "LAN_COMPLETE_LOG_COPY_INVALID", "Complete scenario must copy each returned target log once using the fixed safe delivery name");
  requireContract(packerCalls.length === 1 && packerCalls[0].exit_code === 0, "LAN_COMPLETE_PACKER_CALL_INVALID", "Complete scenario must execute one successful packer call");
  if (listingCalls.length === 1) {
    const parts = listingCalls[0].command.trim().split(" && ");
    const safe = parts.length >= 1 && parts.length <= 2 && parts.every((part) => {
      const match = /^ls -la ([^;&|`\r\n]+)$/u.exec(part);
      if (match === null) return false;
      const target = match[1];
      return target === "output/delivery" || (path.isAbsolute(target) && target.replaceAll("\\", "/").includes("/private/claude-config/") && target.replaceAll("\\", "/").endsWith(`/skills/${GENERATED_SKILL_NAME}/scripts/`));
    });
    requireContract(safe && listingCalls[0].exit_code === 0 && listingCalls[0].ordinal < packerCalls[0].ordinal, "LAN_COMPLETE_LISTING_INVALID", "Optional listing must be read-only, scoped, and occur before packing");
  }
  requireContract(!/[;&|`\r\n]|\$\(/u.test(brokerCalls[0].command ?? ""), "LAN_COMPLETE_BROKER_COMMAND_INVALID", "Broker call must be one unchained command");
  requireContract(/^\s*\/usr\/bin\/python3\s+/u.test(packerCalls[0].command ?? "") && !/[;&|`\r\n]|\$\(/u.test(packerCalls[0].command ?? "") && (packerCalls[0].command ?? "").replaceAll("\\", "/").includes(`/skills/${GENERATED_SKILL_NAME}/scripts/pack_result_zip.py`), "LAN_COMPLETE_PACKER_COMMAND_INVALID", "Packer call must use /usr/bin/python3 and the generated Skill's fixed script as one unchained command");
  const brokerAuditPath = path.join(workspaceRoot, "runtime", "broker-stub-audit.json");
  requireContract(fs.existsSync(brokerAuditPath), "LAN_COMPLETE_BROKER_AUDIT_MISSING", "Broker contract audit is missing");
  const brokerAudit = JSON.parse(fs.readFileSync(brokerAuditPath, "utf8"));
  requireContract(brokerAudit.status === "PASS" && brokerAudit.request.anchors[0].slot === "1" && brokerAudit.request.anchors[1].slot === "2", "LAN_COMPLETE_SLOT_REQUEST_INVALID", "Broker request did not preserve client/server slots");
  const targetPaths = new Set(brokerAudit.result.target_logs.map((item) => path.resolve(workspaceRoot, item.log_path)));
  auditReadScope(processResult, workspaceRoot, targetPaths);
  const outputPath = path.join(workspaceRoot, "output", "result.zip");
  requireContract(fs.existsSync(outputPath) && fs.statSync(outputPath).isFile(), "LAN_COMPLETE_ZIP_MISSING", "Complete scenario did not create output/result.zip");
  const inspect = `import json,sys,zipfile\nfrom pathlib import Path\nwith zipfile.ZipFile(Path(sys.argv[1])) as z:\n print(json.dumps({"names":z.namelist(),"result":z.read("result.txt").decode("utf-8"),"entries":{n:z.read(n).hex() for n in z.namelist()[1:]}}))\n`;
  const inspected = fs.readFileSync(outputPath).length > 0
    ? fs.openSync(outputPath, "r")
    : null;
  if (inspected !== null) fs.closeSync(inspected);
  const zipResult = spawnSync(pythonEntry, ["-I", "-B", "-c", inspect, outputPath], { encoding: "utf8", timeout: 30_000 });
  requireContract(zipResult.status === 0 && zipResult.signal === null, "LAN_COMPLETE_ZIP_INVALID", "result.zip could not be inspected");
  const zip = JSON.parse(zipResult.stdout);
  requireContract(zip.names[0] === "result.txt" && zip.names.length === 3 && zip.result.trim().length > 0, "LAN_COMPLETE_ZIP_SHAPE_INVALID", "result.zip must contain result.txt and two used logs");
  requireContract(zip.names.every((name) => !name.includes("/") && !name.includes("\\")) && zip.names.slice(1).every((name) => name.endsWith(".log")), "LAN_COMPLETE_ZIP_NAMES_INVALID", "result.zip entries must be flat and contain only used logs after result.txt");
  const entryBytes = Object.values(zip.entries).map((hex) => Buffer.from(hex, "hex"));
  const expected = [fs.readFileSync(sourceClient), fs.readFileSync(sourceServer)];
  requireContract(expected.every((bytes) => entryBytes.some((item) => item.equals(bytes))), "LAN_COMPLETE_ZIP_LOG_BYTES_INVALID", "result.zip did not preserve both used target logs");
  const text = finalText(processResult.events);
  const requiredFinalPatterns = [/(?:结论|conclusion)/iu, /(?:关键证据|key evidence)/iu, /(?:证据缺口|evidence gaps?)/iu, /(?:所用日志|使用日志|used logs?)/iu, /LATE_RESPONSE/u, /API_COMPLETE/u, /result\.zip/u];
  const requiredResultPatterns = [/(?:结论|conclusion)/iu, /(?:证据缺口|evidence gaps?)/iu, /(?:所用日志|使用日志|target_logs|used logs?)/iu, /LATE_RESPONSE/u, /API_COMPLETE/u];
  requireContract(requiredFinalPatterns.every((pattern) => pattern.test(text)) && requiredResultPatterns.every((pattern) => pattern.test(zip.result)), "LAN_COMPLETE_FINAL_INVALID", "Complete response and result.txt must directly include the conclusion, evidence, gaps, used logs, and observed markers");
  return { schema_version: 1, status: "PASS", scenario: "complete", helper_calls: 1, broker_calls: 1, packer_calls: 1, slots: ["1", "2"], zip_entries: zip.names, final_response_utf8_size: Buffer.byteLength(text, "utf8") };
}


export async function runDiagnosis(options, { ambient = process.env, onProgress = null } = {}) {
  const workRoot = createEmptyRoot(options.workRoot, `${options.scenario} work root`);
  const privateRoot = createEmptyRoot(options.privateRoot, `${options.scenario} private root`);
  const evidenceRoot = createEmptyRoot(options.evidenceRoot, `${options.scenario} evidence root`);
  const usageRoot = createEmptyRoot(options.usageRoot, `${options.scenario} usage root`);
  const workspaceRoot = path.join(workRoot, "workspace");
  const configRoot = path.join(privateRoot, "claude-config");
  const prepared = prepareWorkspace({ workspaceRoot, configRoot, generatedSkillRoot: options.generatedSkillRoot, helperSkillRoot: options.helperSkillRoot, brokerStub: options.brokerStub, clientLog: options.clientLog, serverLog: options.serverLog });
  const settings = path.join(privateRoot, "claude-settings.json");
  materializeClaudeSettings(options.claudeSettings, settings);
  const home = path.join(privateRoot, "home");
  const temporary = path.join(privateRoot, "tmp");
  for (const directory of [home, temporary]) fs.mkdirSync(directory, { mode: 0o700 });
  const complete = options.scenario === "complete";
  const limits = DIAGNOSIS_LIMITS[options.scenario];
  const processResult = await runClaudeProcess({
    claudeEntry: options.claudeEntry,
    settings,
    cwd: workspaceRoot,
    prompt: diagnosisPrompt(options.scenario),
    phase: complete ? "LAN_DIAGNOSIS_COMPLETE" : "LAN_DIAGNOSIS_MISSING_SLOTS",
    invocationId: `${options.runId}:${options.scenario}`,
    tools: ["Read", "Write", "Skill", "Bash"],
    allowedTools: [
      `Skill(${GENERATED_SKILL_NAME})`,
      ...(complete ? ["Skill(logparse-diagnose)", "Bash(problem-locator-logparse:*)", "Bash(cp:*)", "Bash(ls:*)", "Bash(/usr/bin/python3:*)"] : []),
      "Read",
      "Edit(/output/**)",
    ],
    disallowedTools: ["Glob", "Grep", "WebFetch", "WebSearch"],
    allowToolErrors: false,
    maxTurns: DIAGNOSIS_MAX_TURNS,
    maxBudgetUsd: limits.usd_limit,
    wallTimeoutSeconds: DIAGNOSIS_WALL_SECONDS,
    noProgressSeconds: CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
    tracePath: path.join(evidenceRoot, `${options.scenario}.stream-json.ndjson`),
    stderrPath: path.join(evidenceRoot, `${options.scenario}.stderr.txt`),
    receiptPath: path.join(usageRoot, `${options.scenario}.json`),
    environment: {
      configRoot,
      home,
      temporary,
      pathEntries: [prepared.binRoot],
      brokerEnvironment: complete ? { PROBLEM_LOCATOR_LOGPARSE_ENDPOINT: "http://127.0.0.1:9", PROBLEM_LOCATOR_LOGPARSE_TOKEN: "lan-fast-e2e-contract-token" } : null,
    },
  }, { ambient, onProgress });
  const phase = complete ? "LAN_DIAGNOSIS_COMPLETE" : "LAN_DIAGNOSIS_MISSING_SLOTS";
  writeJsonExclusive(path.join(evidenceRoot, "model-invocations.json"), { schema_version: 1, status: "PASS", retry_policy: "NONE", invocations: [processResult.receipt] });
  const usage = auditInvocationUsage([processResult.receipt], { tokenLimit: limits.token_limit, usdLimit: limits.usd_limit, phases: [phase] });
  writeJsonExclusive(path.join(evidenceRoot, "model-usage.json"), usage);
  const oracle = complete
    ? auditComplete({ processResult, workspaceRoot, pythonEntry: options.pythonEntry, sourceClient: options.clientLog, sourceServer: options.serverLog })
    : auditMissingSlots({ processResult, workspaceRoot });
  writeJsonExclusive(path.join(evidenceRoot, "scenario-evaluation-audit.json"), oracle);
  writeJsonExclusive(path.join(evidenceRoot, "tool-trace-audit.json"), { schema_version: 1, status: "PASS", skills: processResult.skills, bash: processResult.bash.map((item) => ({ ordinal: item.ordinal, command: item.command, exit_code: item.exit_code })), denied: processResult.denied });
  const gate = { schema_version: 1, status: "PASS", goal: "diagnosis", scenario: options.scenario, retry_count: 0, checks: { model: true, slot_contract: true, helper_contract: true, result_delivery: true } };
  writeJsonExclusive(path.join(evidenceRoot, "adapter-receipt.json"), gate);
  return { gate, usage, invocation: processResult.receipt, oracle };
}
