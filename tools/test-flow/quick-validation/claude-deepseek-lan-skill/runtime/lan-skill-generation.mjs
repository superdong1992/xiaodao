import fs from "node:fs";
import path from "node:path";

import { materializeClaudeSettings } from "../../../lib/release-inputs.mjs";
import { runClaudeProcess } from "../../claude-deepseek/runtime/claude-deepseek-process.mjs";
import {
  FIXED_MODULE,
  GENERATED_SKILL_NAME,
  GENERATION_MAX_TURNS,
  GENERATION_TOKEN_LIMIT,
  GENERATION_USD_LIMIT,
  GENERATION_WALL_SECONDS,
  META_SKILL_NAME,
  auditGeneratedPackage,
  auditGenerationTools,
  auditInvocationUsage,
  buildProducerIdentity,
  buildSourceWikiIdentity,
  copyTree,
  createEmptyRoot,
  publishGenerationCache,
  treeDigest,
  validateGeneratedPackage,
  writeJsonExclusive,
  CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
} from "./lan-skill-contract.mjs";


function permissionAbsolute(filePath) {
  const resolved = path.resolve(filePath);
  return `//${resolved.split(path.sep).join("/").replace(/^\/+/, "").replaceAll("\\", "\\\\").replaceAll("(", "\\(").replaceAll(")", "\\)")}`;
}

function prepareWorkspace({ workspaceRoot, configRoot, metaSkillRoot, wiki }) {
  fs.mkdirSync(workspaceRoot, { recursive: true, mode: 0o700 });
  fs.mkdirSync(configRoot, { recursive: true, mode: 0o700 });
  const git = path.join(workspaceRoot, ".git");
  fs.mkdirSync(path.join(git, "objects"), { recursive: true, mode: 0o700 });
  fs.mkdirSync(path.join(git, "refs", "heads"), { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(git, "HEAD"), "ref: refs/heads/main\n", { mode: 0o600, flag: "wx" });
  for (const name of ["inputs", "runtime", "output"]) fs.mkdirSync(path.join(workspaceRoot, name), { mode: 0o700 });
  fs.copyFileSync(wiki, path.join(workspaceRoot, "inputs", "wiki.md"), fs.constants.COPYFILE_EXCL);
  const identity = buildSourceWikiIdentity(fs.readFileSync(wiki));
  writeJsonExclusive(path.join(workspaceRoot, "runtime", "source-wiki-identity.json"), identity);
  const installedSkill = path.join(configRoot, "skills", META_SKILL_NAME);
  copyTree(metaSkillRoot, installedSkill);
  return { identity, installedSkill, output: path.join(workspaceRoot, "output") };
}

export function generationPrompt() {
  return `Use the ${META_SKILL_NAME} Skill to convert inputs/wiki.md into one LAN-direct diagnosis Skill named ${GENERATED_SKILL_NAME}. The user-confirmed fixed module is ${JSON.stringify(FIXED_MODULE)}.

Your first action must call the Skill tool with exactly {"skill":"${META_SKILL_NAME}"}. Then read inputs/wiki.md and runtime/source-wiki-identity.json in full. From the loaded Skill base directory, read references/output-contract.md and assets/pack_result_zip.py in full. Do not read repository files, tests, validators, or oracles. Use only Skill, Read, and Write.

Generate the complete package under output/${GENERATED_SKILL_NAME}. Its root entries must be exactly SKILL.md, methods.json, logparse.json, references, and scripts. Copy identity.sha256 into methods.json, copy every identity.log_templates item into references/source-log-templates.md in exact order, and copy the packer asset byte-for-byte into scripts/pack_result_zip.py. methods.json must begin required_user_inputs with problem_time, client_slot, client_process_name, server_slot, server_process_name; use stable Wiki input IDs service and api rather than service_name or api_name. Every evidence_marker must be the output-contract's canonical stable literal: never use a complete template and never retain a {field} or %x placeholder. A positive Wiki log template whose own fields and calculations can independently confirm more than one cause must appear in every applicable method's evidence_markers. logparse.json must use helper_skill logparse-diagnose, fixed module ${FIXED_MODULE}, and required client/server input mappings. The generated SKILL.md must require all inputs before one Skill(logparse-diagnose) load, delegate to the loaded contract, analyze only target_logs[*].log_path, return a direct conclusion summary, produce result.txt plus result.zip, and include the exact safe copied-log filename format <label>__<module>__slot_<slot>__<process_name>[__pid_<pid>].log. Do not embed old Logparse CLI commands.

Finish all Reads before the first Write. Use one successful Write per final file, keep all Writes contiguous, never overwrite, never write outside output/${GENERATED_SKILL_NAME}, and stop after the final Write. Do not include package contents in the final response.`;
}

function secretScan({ roots, settings }) {
  const value = JSON.parse(fs.readFileSync(settings, "utf8"));
  const canaries = [value.env?.ANTHROPIC_AUTH_TOKEN].filter((item) => typeof item === "string" && item.length >= 8);
  let files = 0;
  const visit = (root) => {
    for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
      const target = path.join(root, entry.name);
      if (entry.isDirectory()) visit(target);
      else if (entry.isFile()) {
        files += 1;
        const bytes = fs.readFileSync(target);
        for (const canary of canaries) if (bytes.includes(Buffer.from(canary))) throw new Error("provider credential leaked into LAN generation evidence");
      }
    }
  };
  roots.filter((root) => fs.existsSync(root)).forEach(visit);
  return { schema_version: 1, status: "PASS", scanned_files: files, canary_count: canaries.length, secret_values_persisted: false };
}

export async function runGeneration(options, { ambient = process.env, onProgress = null } = {}) {
  const workRoot = createEmptyRoot(options.workRoot, "generation work root");
  const privateRoot = createEmptyRoot(options.privateRoot, "generation private root");
  const evidenceRoot = createEmptyRoot(options.evidenceRoot, "generation evidence root");
  const usageRoot = createEmptyRoot(options.usageRoot, "generation usage root");
  const identity = options.claudeIdentity;
  const producer = buildProducerIdentity({ wiki: options.wiki, metaSkillRoot: options.metaSkillRoot, module: FIXED_MODULE, claudeIdentity: identity, runnerFiles: options.runnerFiles });
  const workspaceRoot = path.join(workRoot, "workspace");
  const configRoot = path.join(privateRoot, "claude-config");
  const prepared = prepareWorkspace({ workspaceRoot, configRoot, metaSkillRoot: options.metaSkillRoot, wiki: options.wiki });
  const settings = path.join(privateRoot, "claude-settings.json");
  materializeClaudeSettings(options.claudeSettings, settings);
  const home = path.join(privateRoot, "home");
  const temporary = path.join(privateRoot, "tmp");
  for (const directory of [home, temporary]) fs.mkdirSync(directory, { mode: 0o700 });
  const allowedTools = [
    `Skill(${META_SKILL_NAME})`,
    "Read(/inputs/wiki.md)",
    "Read(/runtime/source-wiki-identity.json)",
    `Read(${permissionAbsolute(path.join(prepared.installedSkill, "references", "output-contract.md"))})`,
    `Read(${permissionAbsolute(path.join(prepared.installedSkill, "assets", "pack_result_zip.py"))})`,
    "Edit(/output/**)",
  ];
  const processResult = await runClaudeProcess({
    claudeEntry: options.claudeEntry,
    settings,
    cwd: workspaceRoot,
    prompt: generationPrompt(),
    phase: "LAN_SKILL_GENERATION",
    invocationId: `${options.runId}:generation`,
    tools: ["Read", "Write", "Skill"],
    allowedTools,
    disallowedTools: ["Bash", "Glob", "Grep", "WebFetch", "WebSearch"],
    maxTurns: GENERATION_MAX_TURNS,
    maxBudgetUsd: GENERATION_USD_LIMIT,
    wallTimeoutSeconds: GENERATION_WALL_SECONDS,
    noProgressSeconds: CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
    tracePath: path.join(evidenceRoot, "generation.stream-json.ndjson"),
    stderrPath: path.join(evidenceRoot, "generation.stderr.txt"),
    receiptPath: path.join(usageRoot, "generation.json"),
    environment: { configRoot, home, temporary },
  }, { ambient, onProgress });
  const tools = auditGenerationTools(processResult);
  const usage = auditInvocationUsage([processResult.receipt], { tokenLimit: GENERATION_TOKEN_LIMIT, usdLimit: GENERATION_USD_LIMIT, phases: ["LAN_SKILL_GENERATION"] });
  writeJsonExclusive(path.join(evidenceRoot, "claude-identity.json"), { schema_version: 1, status: "PASS", claude: identity, producer });
  writeJsonExclusive(path.join(evidenceRoot, "model-invocations.json"), { schema_version: 1, status: "PASS", retry_policy: "NONE", invocations: [processResult.receipt] });
  writeJsonExclusive(path.join(evidenceRoot, "model-usage.json"), usage);
  writeJsonExclusive(path.join(evidenceRoot, "tool-trace-audit.json"), tools);
  const packageRoot = path.join(prepared.output, GENERATED_SKILL_NAME);
  const validator = validateGeneratedPackage({ pythonEntry: options.pythonEntry, validator: path.join(options.metaSkillRoot, "scripts", "validate_generated_skill.py"), packageRoot, wiki: options.wiki, module: FIXED_MODULE });
  writeJsonExclusive(path.join(evidenceRoot, "package-validation-audit.json"), validator);
  const oracle = auditGeneratedPackage({ packageRoot, oraclePath: options.oracle, module: FIXED_MODULE });
  writeJsonExclusive(path.join(evidenceRoot, "scenario-evaluation-audit.json"), oracle);
  const security = secretScan({ roots: [workspaceRoot, evidenceRoot, packageRoot], settings });
  writeJsonExclusive(path.join(evidenceRoot, "security-audit.json"), security);
  const stagingRoot = path.join(options.cacheRoot, ".staging", `lan-generation-${options.runId}`);
  const cache = publishGenerationCache({ cacheRoot: options.cacheRoot, producer, packageRoot, stagingRoot });
  const packageReceipt = { schema_version: 1, status: "PASS", producer_identity: producer.producer_identity, package_tree_sha256: treeDigest(packageRoot), source_wiki_sha256: prepared.identity.sha256, validator, oracle, tools, cache: cache.manifest, published: cache.published };
  writeJsonExclusive(path.join(evidenceRoot, "generated-skill.json"), packageReceipt);
  const gate = { schema_version: 1, status: "PASS", goal: "generation", retry_count: 0, checks: { model: true, validator: true, oracle: true, tool_trace: true, cache: true, security: true } };
  writeJsonExclusive(path.join(evidenceRoot, "adapter-receipt.json"), gate);
  return { gate, producer, cache, usage, invocation: processResult.receipt };
}
