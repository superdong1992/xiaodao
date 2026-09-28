import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const read = (relative) => fs.readFileSync(path.join(ROOT, relative), "utf8").replace(/\r\n/g, "\n");
const paragraphs = (text) => text.split(/\n\s*\n/).map((part) => part.replace(/`/g, "").replace(/\s+/g, " "));
const hasParagraph = (text, ...patterns) => paragraphs(text).some((part) => patterns.every((pattern) => pattern.test(part)));
const shellCommands = (text) => [...text.matchAll(/^```(?:bash|sh|shell)\n([\s\S]*?)^```/gm)]
  .flatMap((match) => match[1].replace(/\\\n\s*/g, " ").split("\n"))
  .map((line) => line.trim()).filter((line) => line && !line.startsWith("#"));

test("Redis 部署仍明确要求 PostgreSQL 和 DATABASE_URL", () => {
  const redis = read("docs/website-redis-deployment.md");
  assert.doesNotMatch(redis, /不引入\s*PostgreSQL|(?:不需要|无需)\s*`?DATABASE_URL`?/u,
    "Redis 登录配置不能宣称当前服务不需要 PostgreSQL 或 DATABASE_URL");
  assert.match(redis, /PostgreSQL/);
  assert.match(redis, /DATABASE_URL/);
  assert.ok(hasParagraph(redis, /DATABASE_URL/, /必填|必须|需要配置|不可缺少/u),
    "Redis 部署说明必须明确数据库连接配置是启动前提");
  assert.match(redis, /production-upgrade-2026-09-23\.md|postgresql-migration\.md/);
});

test("追问身份文档区分默认 Redis 与 trusted_header 兼容模式", () => {
  const followup = read("docs/website-report-followup.md");
  assert.ok(hasParagraph(followup, /redis/i, /默认/u), "追问文档应注明默认 Redis 会话模式");
  assert.match(followup, /user\.userid/);
  assert.match(followup, /Cookie/);
  assert.match(followup, /trusted_header/);
  const accessParagraphs = paragraphs(followup).filter((part) => /access\.authenticate\s*\(/.test(part));
  assert.ok(accessParagraphs.length > 0, "应保留 trusted_header 模式的既有认证接线说明");
  for (const part of accessParagraphs) {
    assert.match(part, /trusted_header/, "access.authenticate 仅属于 trusted_header 模式，必须在同一说明中限定范围");
  }
  assert.doesNotMatch(followup, /BFF[^。\n]*四个路由都[^。\n]*access\.authenticate/u,
    "不能把兼容模式认证说成所有 BFF 路径的默认要求");
  assert.ok(hasParagraph(followup, /默认/u, /BFF/, /不(?:自动)?(?:执行|验证|校验)\s*(?:CSRF|X-CSRF-Token)/iu, /网站/u, /保留|自行/u),
    "必须说明默认 BFF 不自动校验 CSRF，网站仍须保留自己的校验入口");
});

test("历史数据升级只产出中间 SQLite，当前服务还需 PostgreSQL 导入", () => {
  const legacy = read("docs/data-upgrade-v11-r2.md");
  assert.match(legacy, /problem-locator-data-upgrade/);
  assert.match(legacy, /problem-locator-postgres-import/);
  assert.match(legacy, /postgresql-migration\.md/);
  assert.ok(hasParagraph(legacy, /SQLite/, /中间|历史|旧版/u), "旧升级工具的 SQLite 产物必须标为中间或历史格式");
  const explicitlyRejectsDirectStart = hasParagraph(legacy, /PostgreSQL|当前版本|当前服务/u, /不能|不可|不得|不要/u, /直接/u, /启动|使用|打开/u);
  const confinesDirectUseToHistoricalServer = hasParagraph(legacy, /PostgreSQL/, /保持停服/u, /problem-locator-postgres-import/)
    && hasParagraph(legacy, /只有/u, /历史.*SQLite/u, /才可直接/u);
  assert.ok(explicitlyRejectsDirectStart || confinesDirectUseToHistoricalServer,
    "必须区分当前 PostgreSQL 导入步骤和只有历史 SQLite 服务可直接使用的中间目录");
  assert.doesNotMatch(legacy, /DATA_ROOT`?\s*改为目标目录[，,]\s*再启动\s*8\.2/u,
    "不能保留只切换 DATA_ROOT 就启动当前版本的旧指令");
});

test("累计生产升级指南覆盖数据库、Redis 登录、专用路由和报告追问", () => {
  const guide = read("docs/production-upgrade-2026-09-23.md");
  assert.ok(hasParagraph(guide, /DATABASE_URL/, /必填|必须|需要配置|不可缺少/u), "DATABASE_URL 必须明确为必填配置");
  assert.match(guide, /DATABASE_POOL_SIZE/);
  assert.match(guide, /PostgreSQL\s*17/);
  assert.ok(hasParagraph(guide, /redis/i, /默认/u), "应注明默认 Redis 登录模式");
  assert.match(guide, /trusted_header/);
  assert.match(guide, /user\.userid/);
  assert.match(guide, /REPORT_FOLLOWUP_ENABLED/);
  assert.match(guide, /target_run_id/);
  assert.match(guide, /registration-template\.json@2/);
  assert.match(guide, /routing\.applicability/);
  assert.match(guide, /routing\.exclusions/);
  assert.ok(hasParagraph(guide, /ROUTE/, /assessments/, /0\.95/), "专用路由必须说明 ROUTE 响应和准入门槛");
  assert.match(guide, /assessments/);
  assert.match(guide, /OUTCOME_INVALID/);
  assert.match(guide, /0\.95/);
  assert.match(guide, /Linux/);
  assert.ok(hasParagraph(guide, /单实例|单进程|一个.*(?:Server|服务实例)/u), "数据库并发不代表当前服务支持多实例部署");
});

test("生产迁移命令按计划、执行、校验、启动排序并从环境读取数据库凭据", () => {
  const commands = shellCommands(read("docs/production-upgrade-2026-09-23.md"));
  const plan = commands.findIndex((line) => /\bproblem-locator-postgres-import\b/.test(line) && /--plan-only\b/.test(line));
  const execute = commands.findIndex((line) => /\bproblem-locator-postgres-import\b/.test(line) && /--execute\b/.test(line));
  const validate = commands.findIndex((line) => /\bvalidate-state\b/.test(line));
  const serve = commands.findIndex((line) => /\bserve\b/.test(line));
  assert.ok(plan >= 0 && plan < execute && execute < validate && validate < serve,
    "迁移示例必须先 --plan-only，再 --execute、validate-state，最后 serve");
  for (const command of [commands[plan], commands[execute]]) {
    assert.match(command, /--database-url-env\s+DATABASE_URL\b/, "迁移工具应接收环境变量名，不把连接串写进命令参数");
    assert.match(command, /--source-data-root\b/);
    assert.match(command, /--target-data-root\b/);
  }
  assert.match(commands[validate], /--data-root\b/);
  assert.match(commands[serve], /--env-file\b/, "启动示例应显式加载私有配置文件");
});

test("生产升级保留原目录和完整备份，并说明新数据不能自动回退", () => {
  const guide = read("docs/production-upgrade-2026-09-23.md");
  assert.ok(hasParagraph(guide, /备份/u, /原|旧|完整/u), "升级前必须保留原数据的完整备份");
  assert.ok(hasParagraph(guide, /原目录|源目录|旧目录/u, /保留|不.*修改|不.*删除/u), "迁移必须保留原数据目录");
  assert.match(guide, /pg_dump/);
  assert.ok(hasParagraph(guide, /新(?:业务)?数据|新记录|写入/u, /回退|回滚/u, /不|另行|额外/u),
    "必须说明新业务写入后的回退边界，不能暗示自动同步回旧库");
});
