# tools/test-flow 裁剪与优化 —— 实施说明书

> 本文档写给负责编码实现的 AI/工程师(执行者可能没有本次分析讨论的上下文),目标是让执行者
> 不需要额外调查就能按顺序、安全地完成每一项改动。所有事实性断言(行号、引用关系、提交次数)
> 都已经过直接读取源码/配置/git 历史验证,不是推测。请严格按"排除范围"和"任务列表"执行,
> 不要额外扩大范围或"顺手"重构未列出的内容。

## 0. 背景(执行前必读)

仓库根目录:项目名 `problem-locator`,一个 AI 驱动的日志/故障定位产品,5 周历史(2026-07-25
至今),247 次 git 提交。产品代码 `src/` 共 67,796 行。

`tools/test-flow/` 是这个项目自建的测试/发布验证编排器(Node.js),核心概念是
`Goal → Proof → Stage → Gate → Gate receipt` 的聚合链路(完整设计见
`design/test-flow-architecture.md`,执行者不需要通读,本文档已经摘出所有必要信息):

- **Goal**(如 `dev.default`)由若干 **Proof** 组成(`config/proofs.v2.json`)。
- 每个 **Proof** 绑定一个或多个 **Stage**(`config/stages.v2.json`)。
- 每个 **Stage** 包含若干 **Gate**(`config/gates.v2.json`),Gate 是实际执行校验的最小单元
  (跑 pytest、跑 node test、跑一次真实模型调用等)。
- 六份配置文件(`proofs.v2.json`、`stages.v2.json`、`gates.v2.json`、`identities.v2.json`、
  `policy.v2.json`、`runtime-profiles.v2.json`)共同定义整个系统,互相用字符串 id 引用。

规模现状:`tools/test-flow` 总计约 57,627 行(核心引擎 `lib/` 12,486 行、平台适配器
`adapters/`+运行时脚本 `runtime-support/`+`quick-validation/` 约 30,255 行、框架自身测试套件
`tests/` 14,451 行),超过产品代码一半。这次改动的目标是:**在不削弱真实保障的前提下,消除
已验证的重复/死代码,并新增一个能真正节省开发者时间的快速反馈路径。**

**通用执行纪律**:
1. 严格按"任务列表"顺序执行,每完成一个任务就跑该任务标注的验收命令,通过后再进入下一个。
2. 不要修改"第 1 节:明确排除范围"里列出的任何文件,即使看起来相关或可以顺手改善。
3. 除任务列表明确要求的改动外,不要额外重构、重命名、加注释或"优化风格"。
4. 所有改动必须是行为不变的搬家/去重/删除,除了任务 T1(新增 Goal,是唯一一个新增能力)。
5. 任何一步验收失败,停止并报告失败的具体命令和输出,不要跳过或静默处理。

## 1. 明确排除范围(不要修改)

以下范围经过与仓库所有者确认,本次不涉及,原因写明供理解上下文,**不是待办**:

- **`tools/test-flow/adapters/windows-linux-release.mjs`、`windows-process.ps1`、
  `adapters/linux-linux-release.mjs`**(Windows 原生适配器与纯 Linux Client 适配器):
  曾怀疑从未验证通过,仓库所有者确认当前这些路径测试可以通过,不需要改动。
- **`tools/test-flow/quick-validation/wsl/`**(容器化 fast-e2e,目录名含 "wsl" 但实际通过
  Docker/Colima 运行):虽然没有被六份 `config/*.v2.json` 引用,但仓库所有者确认这是日常在用
  的工具,用来快速验证定位框架能否找到问题。**不要删除、不要移动、不要重构。**
- **`tools/test-flow/quick-validation/codex-luna/run.mjs`、
  `quick-validation/claude-deepseek/run.mjs`**(原生 macOS 直跑路径):已知会导致资源泄露和
  目录权限异常,仓库所有者已暂停使用,**不要分析、不要修改、不要"顺手修复"这个已知问题**。
- **`tools/test-flow/adapters/cross-job-core.mjs`**(3,696 行):虽然内部可以按关注点拆分
  提升可读性,但它是上面几条平台路径的公共底层,本次不动。
- 除本文档任务列表明确列出的文件外,`tools/test-flow` 下的其他所有文件都不在本次范围内。

## 2. 任务列表

### T1(优先级最高)新增快速内环 Goal

**问题**:当前 `dev.default` 要求同时满足 `proof.deterministic-affected` 和
`proof.deterministic-full`(见 `tools/test-flow/config/proofs.v2.json` 的
`goals.dev.default.required_proofs`)。`deterministic.full` 这个 Stage
(`config/stages.v2.json`)的 5 个 Gate 里,`det.contracts`/`det.unit`/`det.integration`/
`det.journey.same-job` 对改动范围完全无感知,永远跑 `tests/deterministic/` 下
contracts/unit/integration/journey 四个目录的全部约 2600+ 个测试;`deterministic.affected`
自身 `reuse` 字段是 `{"dev": "never"}`,从不复用。结果是:**任何一次改动,不管大小,都会触发
完整套件的一次全量执行**,`det.affected` 只是加在全量前面的一层,不产生任何跳过效果。

**要做的事**:在 `tools/test-flow/config/proofs.v2.json` 的 `goals` 对象里新增一个 Goal,
建议命名 `dev.quick`(如果这个名字和现有约定冲突,选择其他清晰的名字,但必须以 `dev.` 开头
以落在 `dev` track 下):

```json
"dev.quick": {
  "description": "Fast, incomplete feedback for the active edit — runs only the affected test scope. Does NOT replace dev.default before commit/release.",
  "tracks": ["dev"],
  "required_proofs": [
    "proof.framework",
    "proof.repository-static",
    "proof.deterministic-affected"
  ],
  "selectable_proofs": []
}
```

这三个 proof id 已经存在(`dev.default` 也在用),不需要新建 proof。`stages.v2.json`/
`gates.v2.json` 不需要任何改动 —— `deterministic.affected` 这个 Stage 的 `depends_on` 只有
`["repository.static"]`,不依赖 `deterministic.full`,所以单独拉出来就是一个完整、可独立运行
的闭包。

`det.affected` gate 的执行逻辑(`tools/test-flow/lib/actions.mjs` 里 `executeGate` 函数,
约第 4133-4144 行,`gate.selector_mode === "affected"` 分支)已经会调用
`planAffectedSelection(context.repoRoot, context.changedFiles)`(定义在
`lib/actions.mjs:1347`,依赖 `affectedSelectors()` 定义在 `lib/actions.mjs:1321`)。这个函数
在改动范围太大时(比如改了 `pyproject.toml`/`uv.lock`)会返回 `selection.defer_to_full = true`,
此时当前代码只是静默返回一个 0 测试的 `NOT_REQUIRED` 状态。**新增一步**:当
`selection.defer_to_full` 为 true 时,在这个 Goal 的运行结果里明确输出提示文字,例如
"改动范围超出快速检查能力,请运行 `dev.default` 获取完整结果",不要让开发者误以为 0 测试
等于"改动是安全的"。这个提示可以加在 `executeGate` 返回 `NOT_REQUIRED` 之后、结果被写入
run 摘要之前的位置,具体挂载点由实现者根据 `lib/status.mjs`/`lib/engine.mjs` 里摘要渲染的
现有模式决定,保持和其他 gate 提示信息一致的格式。

最后,在 `tools/test-flow/README.md` 里加一小节说明 `dev.quick` 的用途和限制(不完整、仅用于
日常迭代、提交/发布前仍需 `dev.default`),放在现有"Dev 确定性测试"小节之后。

**验收标准**:
1. `./tools/test-flow/run.sh --track dev --goal dev.quick --plan-only` 能成功输出计划,
   且计划里出现的 proof 只有 `proof.framework`、`proof.repository-static`、
   `proof.deterministic-affected` 三个。
2. 临时修改一个只属于 `src/problem_locator/domain/` 下的文件(比如加一行空白),运行
   `./tools/test-flow/run.sh --track dev --goal dev.quick`,确认实际执行的 pytest 选择器
   只包含 `tests/deterministic/unit/domain` 和对应集成测试,而不是全部 2600+ 用例(可以从
   Gate 的 `pytest-summary.json` 里的 `tests` 数量看出明显小于全量)。改完记得撤销这个临时
   修改。
3. 临时修改 `pyproject.toml`(比如加一行空白注释性质的改动,不影响解析),运行同样的命令,
   确认命中 `defer_to_full` 分支并展示了新增的提示文字。改完撤销这个临时修改。
4. 运行 `./tools/test-flow/run.sh --track dev --goal dev.default --plan-only`,确认输出和
   改动前完全一致(`dev.default` 不受影响,新 Goal 是平行新增,不是替换)。

### T2 修复重复且不等价的 canonical JSON 实现

**问题**:`tools/test-flow/runtime-support/codex-luna-contract.mjs` 第 118-124 行自己实现了
一份 `canonicalJson`:

```js
export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  if (isPlainObject(value)) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}
```

而 `tools/test-flow/lib/util.mjs` 第 31-33 行的规范实现是:

```js
export function canonicalJson(value) {
  return `${JSON.stringify(canonicalize(value))}\n`;
}
```

两者对同一个逻辑值会产出不同字节(至少末尾换行符不同,且实现路径完全独立),导致
`sha256Bytes()` 算出不同的哈希。这个系统的信任模型建立在"哈希精确一致"上,这是一个真实的
正确性 bug。

**要做的事**:
1. 删除 `tools/test-flow/runtime-support/codex-luna-contract.mjs` 第 118-131 行本地定义的
   `canonicalJson`、`sha256Bytes`、`sha256File`(用 `grep -n "sha256Bytes\|sha256File" tools/test-flow/runtime-support/codex-luna-contract.mjs` 确认这三个函数的准确边界后再删)。
2. 在文件顶部的 import 区域,改为从 `../lib/util.mjs` 导入这三个函数:
   `import { canonicalJson, sha256Bytes, sha256File } from "../lib/util.mjs";`
   (`tools/test-flow/quick-validation/claude-deepseek/runtime/` 下的文件已经是这个 import
   模式,可以参考其写法保持风格一致。)
3. 全局搜索 `codex-luna-contract.mjs` 里对这三个函数的调用点,确认没有依赖旧实现"无尾部换行"
   这个具体字节形状的地方(理论上不应该有,因为所有消费方都应该只关心"哈希是否一致"而不是
   具体字节)。

**验收标准**:
1. `node --test tools/test-flow/tests/codex-luna-contract.test.mjs` 全部通过。
2. `node --test tools/test-flow/tests/` (完整框架自测)全部通过。
3. `grep -n "function canonicalJson\|function sha256Bytes\|function sha256File" tools/test-flow/runtime-support/codex-luna-contract.mjs` 应该没有输出(确认本地定义已删除,只剩 import)。

### T3 去重 `validOutputTokenCapEvidence`

**问题**:`tools/test-flow/lib/engine.mjs` 第 213-236 行和
`tools/test-flow/lib/evidence.mjs` 第 302-325 行是同一个函数体的逐字节相同拷贝(已直接 diff
确认)。

**要做的事**:
1. 保留 `evidence.mjs` 里的版本(evidence/verdict 语义本来就该由这个模块拥有)。
2. 删除 `engine.mjs` 第 213-236 行的定义。
3. 在 `engine.mjs` 顶部 import 区域加上从 `evidence.mjs` 导入
   `validOutputTokenCapEvidence`;检查 `engine.mjs` 里原有的调用点(约第 275 行附近,用
   `grep -n "validOutputTokenCapEvidence" tools/test-flow/lib/engine.mjs` 定位)改为使用
   导入的版本。
4. 检查 `evidence.mjs` 是否已经 `export` 这个函数(第 302 行的 `export function` 前缀应该
   已经有),确认可以被 `engine.mjs` 直接导入,不需要额外改动 `evidence.mjs`。

**验收标准**:
1. `node --test tools/test-flow/tests/engine-usage.test.mjs` 全部通过(注意:这个文件里
   原本约一半用例是专门验证两份实现结果一致的,去重后这些用例的意义变化了,允许它们继续
   通过即可,不需要主动删除或改写这个测试文件——那是 Tier 2 里可选的后续工作,不是本任务
   的一部分)。
2. `node --test tools/test-flow/tests/` 全部通过。
3. `grep -n "function validOutputTokenCapEvidence" tools/test-flow/lib/engine.mjs` 应该没有
   输出。

### T4 删除两个空的孤儿 fixture 目录

**要做的事**:删除 `tools/test-flow/fixtures/macos-codex-luna-client-skill/` 和
`tools/test-flow/fixtures/macos-codex-luna-service-skill/` 这两个目录。执行前用
`find tools/test-flow/fixtures/macos-codex-luna-client-skill tools/test-flow/fixtures/macos-codex-luna-service-skill -type f`
确认两个目录下确实没有任何文件,再用
`git status tools/test-flow/fixtures/` 确认它们没有被 git 跟踪(不会出现在 `git status`
里),然后删除。

**验收标准**:`node --test tools/test-flow/tests/` 全部通过(这两个目录本来就没有被任何测试
引用,预期无影响)。

### T5 删除失效的 `admission_blocker` 处理逻辑并更新文档

**问题**:`tools/test-flow/lib/planner.mjs` 第 589-592 行处理 `stage.admission_blocker`
字段,但 `grep -rn "admission_blocker" tools/test-flow/config/*.v2.json` 返回空 ——
六份配置里没有任何一个 Stage 设置这个字段。`design/test-flow-architecture.md` 第 50-59 行、
第 170 行描述几个 Goal(`dev.macos-codex-luna-e2e`、`dev.macos-claude-deepseek-e2e`、
`release.full`、`release.codex-luna-methods`)"当前显式阻止"
(`EVIDENCE_V2_REAL_DIAGNOSIS_ADAPTER_UNMIGRATED`),这段描述已过时:
`release.codex-luna-methods` 现在甚至不在 `proofs.v2.json` 的 `goals` 列表里。

**要做的事**:
1. 删除 `planner.mjs` 里读取/处理 `stage.admission_blocker` 的代码块(约第 589-592 行,
   用 `grep -n "admission_blocker" tools/test-flow/lib/planner.mjs` 确认完整边界,包括
   可能存在的相关类型定义或注释)。
2. 更新 `design/test-flow-architecture.md`:删除或改写第 50-59 行、第 170 行提到
   "当前显式阻止"的段落,改为反映当前实际状态(这几个 Goal 现在是否已经可以正常运行,
   需要执行者结合 `proofs.v2.json`/`stages.v2.json` 当前内容核实后如实描述,不要凭空写
   "已解除阻止"这类结论性文字,除非确认属实)。

**验收标准**:
1. `node --test tools/test-flow/tests/` 全部通过。
2. `./tools/test-flow/run.sh --track dev --goal dev.default --plan-only` 正常输出,不报错。
3. `grep -n "admission_blocker" tools/test-flow/lib/planner.mjs` 应该没有输出。

### T6 删除未使用的 `methodsSkillRuntimeRefId` 别名

**问题**:`tools/test-flow/lib/release-case.mjs` 第 81 行:
`export const methodsSkillRuntimeRefId = diagnosisSkillRuntimeRefId;` ——
除了它自己的测试(`tools/test-flow/tests/release-case.test.mjs` 第 16 行 import、第 73 行
使用)之外,全仓库没有其他调用点(已用
`grep -rn "methodsSkillRuntimeRefId" tools/test-flow --include="*.mjs"` 确认)。

**要做的事**:
1. 删除 `release-case.mjs` 第 81 行这个别名导出。
2. 修改 `tests/release-case.test.mjs` 第 16 行的 import 和第 73 行的调用,改用
   `diagnosisSkillRuntimeRefId`(需要确认这个名字在 `release-case.mjs` 里已经是
   export 的,如果不是则改为 export)。

**验收标准**:`node --test tools/test-flow/tests/release-case.test.mjs` 通过。

### T7 删除信噪比最低的测试文件

**要做的事**:删除 `tools/test-flow/tests/cross-job-polling.test.mjs`(30 行)。这个文件
断言一个模板函数返回它自己硬编码的字面量,没有独立的校验来源。删除前用
`grep -rn "cross-job-polling" tools/test-flow --include="*.mjs" --include="*.json"` 确认
没有其他地方引用这个测试文件本身(测试文件一般不会被引用,只是确认一下)。

**验收标准**:`node --test tools/test-flow/tests/` 全部通过,测试总数比删除前少(具体减少
数量取决于该文件里的用例数)。

### T8 删除未被读取的 schema 文档

**问题**:以下 5 个 `*.schema.json` 文件已确认没有任何 `.mjs` 校验逻辑在运行时读取它们
(实际强制校验 100% 手写在对应的 `.mjs` 文件里,这些 schema 文件只是从未被验证过是否和
实际校验逻辑保持同步的文档):
- `tools/validation/release-verdict.schema.json`
- `tools/validation/core-verdict.schema.json`
- `tools/validation/model-cert.schema.json`
- `tools/validation/model-cert-input.schema.json`
- `tools/test-flow/schemas/failure-diagnostic.schema.json`

**要做的事**:
1. 对每个文件,先运行
   `grep -rln "<文件名>" --include="*.mjs" tools/test-flow tools/validation src | grep -v "\.test\.mjs"`
   重新确认确实没有非测试代码读取它(防止仓库在本次分析之后有新改动)。
2. 确认无引用后删除这 5 个文件。
3. 删除 `tools/test-flow/tests/evidence-v2-certification.test.mjs` 里专门断言这些 schema
   文档自身属性的用例(参考位置约第 1182-1196 行,执行时用
   `grep -n "schema" tools/test-flow/tests/evidence-v2-certification.test.mjs` 定位实际
   断言 schema 文件内容的测试块并删除,不要删除其他验证真实行为的用例)。
4. 确认 `tools/validation/README.md`(如果存在)没有需要同步更新的引用。

**验收标准**:
1. `node --test tools/test-flow/tests/` 全部通过。
2. `find tools/validation tools/test-flow/schemas -name "*.schema.json"` 应该没有输出。

## 3. Tier 2 —— 结构性简化(建议在 T1-T8 全部完成并验证通过后再做)

这一组改动体量更大、涉及仓库改动最频繁的文件,建议一次只做一项、每项做完都跑一次完整验证
(见第 4 节),不要合并成一次大改动。

### T9 拆分 `lib/actions.mjs`

`tools/test-flow/lib/actions.mjs` 共 4,210 行,是全仓库改动最频繁的单个文件(247 次提交里
30 次改过它)。其中约第 1887-3830 行(约 1,943 行)是只服务 Codex-Luna 和 Claude-DeepSeek
E2E 认证的逻辑,和 `dev.default` 每次都会跑到的核心 pytest/进程检查代码(文件前半部分)
混在一起。

**要做的事**:把这约 1,943 行按 provider 拆分成两个新文件,建议
`tools/test-flow/lib/actions-codex-luna.mjs` 和 `tools/test-flow/lib/actions-claude-deepseek.mjs`
(具体切分点由实现者读取这段代码后按函数边界确定,不要在函数中间切断)。`actions.mjs` 保留
文件前半部分(pytest/node-test/repository-check 核心执行逻辑)和顶层 `executeGate` 分发,
在需要调用被拆分出去的逻辑时改为 import。**纯代码搬家,不允许改变任何函数的行为或签名**,
除非某个函数被两个新文件都需要,此时把它留在 `actions.mjs` 里公共导出,或者提到
`lib/util.mjs`(如果它本质上是通用工具函数)。

**验收标准**:
1. `git diff --stat` 里 `actions.mjs` 应该主要是删除行,新文件应该主要是新增行;不应该有
   大段"删除又新增但内容不同"的 diff(说明逻辑被改写而不是搬运)。
2. `node --test tools/test-flow/tests/` 全部通过,测试总数不变。
3. `./tools/test-flow/run.sh --track dev --goal dev.default --plan-only` 输出和改动前完全
   一致。

### T10 折叠 `identities.v2.json` 里的单用 identity set

**问题**:`tools/test-flow/config/identities.v2.json` 定义了 21 个 identity set,已确认
其中 19 个只被 `stages.v2.json` 里唯一一个 Stage 引用(只有 2 个 set 被 2 个以上 Stage
共用)。这层间接对复用的贡献很小。

**要做的事**:对每个只被单一 Stage 引用的 identity set,把它的内容直接内联进那个 Stage 的
定义里(具体字段结构参考 `lib/config.mjs` 里 `identity_set` 的解析逻辑,确认内联后的字段名
和结构与解析器期望的一致),然后从 `identities.v2.json` 里删除这个 set。保留那 2 个真正被
多个 Stage 共用的 identity set。

**验收标准**:
1. `node --test tools/test-flow/tests/config-contract.test.mjs` 和
   `tools/test-flow/tests/config-planner.test.mjs` 全部通过(这两个文件是配置交叉校验器
   自身的测试,如果内联后的结构不对会在这里报错)。
2. `./tools/test-flow/run.sh --track dev --goal dev.default --plan-only` 输出的 identity
   摘要和改动前一致。

### T11 评估折叠 `lib/failure-diagnostic.mjs`

`tools/test-flow/lib/failure-diagnostic.mjs`(233 行)只服务两个 Gate(`TARGET_GATE` 常量,
约第 33-36 行定义),和 `planner.mjs` 里已经通用的 `retryRequirement`、`history.mjs` 的
`failureFingerprint` 功能有重叠。**这一项需要实现者先读完 `failure-diagnostic.mjs` 全文和
它两个消费方的调用方式,确认它的独特逻辑是否真的可以完全被现有的通用重试/诊断机制覆盖**;
如果可以,折叠成 `planner.mjs` 或 `history.mjs` 里的一个小 helper 函数并删除这个文件;
如果发现有它独有的、必要的逻辑,保留文件但在文件顶部加一句注释说明为什么它没有被折叠
(仅在这一种情况下允许加注释)。

**验收标准**:无论选择哪个方向,`node --test tools/test-flow/tests/` 全部通过,且
`det.evidence-v2-core`、`det.contracts` 等依赖失败诊断路径的 Gate 行为不变。

### T12 迁移 `lib/release-inputs.mjs` 到子目录

`tools/test-flow/lib/release-inputs.mjs`(934 行)只在 release/E2E 认证类 Goal 执行时用到,
`dev.default` 的日常路径不会触发它(`planner.mjs` 里
`claudeRuntimeRequired`/`serverRuntimeRequired`/`logparseRuntimeRequired` 对 `dev.default`
的 Stage 集合均为 false)。

**要做的事**:创建 `tools/test-flow/lib/release/` 目录,把 `release-inputs.mjs` 移动到
`tools/test-flow/lib/release/inputs.mjs`,更新所有 import 这个文件的地方(用
`grep -rln "release-inputs" tools/test-flow --include="*.mjs"` 找到全部引用点)改为新路径。

**验收标准**:`node --test tools/test-flow/tests/` 全部通过;
`./tools/test-flow/run.sh --track dev --goal dev.default --plan-only` 和
`--goal release.full --plan-only` 都能正常输出。

### T13 简化框架自身测试套件里的冗余断言(可选,建议在 T1-T12 都完成后再做)

以下每一项都是"不改变被测行为,只减少测试自身冗余"的整理,互相独立,可以按需选做:

- `tools/test-flow/tests/release-inputs.test.mjs`(810 行,20 次提交,是该测试目录里改动
  最频繁的文件):把其中散落的十几个精确哈希/版本号断言(比如
  `RELEASE_UV_VERSION`、`CODEX_LUNA_EXPECTED_CLI_SHA256` 这类)合并成一次"冻结 manifest
  比对",保留真正的行为覆盖(Docker identity drift、Headless Shell smoke、adapter 矩阵
  遍历这些用例不要动)。
- `tools/test-flow/tests/codex-luna-contract.test.mjs`(697 行):把纯粹断言"identity 维度
  是冻结常量"的用例和真正测试逻辑(usage 计价公式、RPC marker 映射)的用例分成两个
  `describe` 块或两个文件,便于以后区分维护。
- `tools/test-flow/tests/rest-api-guide.test.mjs`(242 行)和
  `tools/test-flow/tests/docs-drift.test.mjs`(72 行)里断言 README 小节内容和 OpenAPI
  schema/代码保持同步的部分:与其手写测试逐字段比对,不如写一个小脚本从 schema 直接生成
  对应的 Markdown 表格,测试只需确认"生成结果和文件内容一致"(防止手改文档却忘记同步)。
  `docs-drift.test.mjs` 里的链接检查和"废弃词汇"检查不属于这一项,不要动。

**验收标准**:每改一个文件跑一次 `node --test <该文件路径>` 确认通过,再跑一次完整
`node --test tools/test-flow/tests/` 确认没有破坏其他文件。

## 4. 端到端验证(全部任务完成后)

1. `node --test tools/test-flow/tests/`(或仓库约定的框架自测命令,以
   `tools/test-flow/README.md` 里的说明为准)全部通过。
2. `./tools/test-flow/run.sh --track dev --goal dev.default --plan-only` 正常输出,Goal/
   Proof/Stage/Gate 数量与改动前一致(T1 新增的 `dev.quick` 除外,那是预期的新增)。
3. `./tools/test-flow/run.sh --track dev --goal dev.default` 真实执行一次,全部 Gate PASS。
4. `./tools/test-flow/run.sh --track dev --goal dev.quick --plan-only` 和真实执行一次,
   按 T1 的验收标准逐条确认。
5. `git diff --stat` 通读一遍改动清单,确认没有超出本文档任务列表范围的文件被改动。

## 5. 明确保留(不要在"顺手优化"的冲动下改动)

- `lib/engine.mjs`、`planner.mjs`、`config.mjs`、`identity.mjs`、`evidence.mjs`(finalize/
  verify 部分)、`status.mjs`、`history.mjs`、`util.mjs`、`source-snapshot.mjs`、
  `process.mjs`、`usage.mjs`、`resources.mjs`:每次运行都会执行到,体量和职责成正比,这套
  字节级精确哈希机制曾经真实抓到过一个线上 bug(`FIXED_ISSUES.md` 里的 `PL-FIX-029`)。
- `checkpoint.mjs` 里手写的 TAR writer:看起来像重复造轮子,但全仓库没有任何
  `package.json`/npm 依赖,手写是为了不引入外部包,和项目整体的供应链选择一致,不要换成
  npm 的 `tar` 包。
- `tools/test-flow/tests/` 里体量最大的几个文件(`actions.test.mjs`、
  `codex-luna-app-server.test.mjs`、`evidence-v2-certification.test.mjs`、
  `evidence.test.mjs`、`cross-job-runtime-boundary.test.mjs` 等):都在守护真实风险(模型
  调用花的真金白银、防篡改、隔离 agent 的密钥泄露、checkpoint 数据丢失),体量由外部协议/
  风险复杂度决定,不是注水,不要因为"文件很长"就尝试精简。
- 第 1 节列出的所有平台适配器和 quick-validation 相关内容。
