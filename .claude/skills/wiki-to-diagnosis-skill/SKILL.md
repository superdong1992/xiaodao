---
name: wiki-to-diagnosis-skill
description: Convert a non-sensitive Markdown issue-location wiki into a deterministic repo-local Problem Locator diagnose-* Skill at semantic version 2.x, including exact diagnosis-skill.json metadata and S00-compliant four-result workflow instructions. Use when creating or updating a Diagnosis Skill from a wiki.
---

# Wiki To Diagnosis Skill

Convert one Markdown issue-location wiki into a bounded `diagnose-*` product. This
generator is version `2.0.0`.

Read [the generated product contract](references/generated-skill-contract.md)
before drafting. Read [the wiki template](references/wiki-template.md) when the
source does not clearly identify target roles, evidence rules, or analysis steps.

## Required inputs

Collect these before drafting:

- A non-sensitive Markdown wiki path or pasted Markdown.
- A stable lower-kebab capability and a non-sensitive Router summary.
- A concrete logparse module and fixed `logparse_product`, when logs are needed.
- The fixed logparse version's supported ContentType values in declaration order.
- A target diagnosis Skill semantic version, starting at `2.0.0`.

Do not infer a concrete module from a placeholder. Do not copy credentials,
customer identifiers, production logs, private endpoints, or sensitive wiki prose
into a generated product or Router summary.

## Confirmation gate

1. Parse and normalize the wiki without changing a `diagnose-*` directory.
2. Present a draft with the Skill id/version, capability, summary, fixed module,
   fixed logparse product, problem scope, roles, allowed ContentTypes, custom
   parameters, assumptions, and analysis rules.
3. Wait for explicit confirmation of the whole draft.
4. Revise and reconfirm after any requested semantic change.
5. Generate only after confirmation.
6. Validate the result and fix every failure.

Silence or an unrelated reply is not confirmation. A change to product semantics
requires an explicit version increase; never overwrite different bytes under the
same `{id,version}`.

## Extract wiki facts

Extract only supported facts:

- `chinese_title`
- `skill_name`: `diagnose-<english-topic-slug>`, lowercase and at most 64 chars
- `module_name`
- `problem_scope`
- ordered target roles: label, Chinese description, required/optional
- task-level single-line custom parameters
- time characteristics
- analysis steps
- judgement rules
- output requirements
- explicit assumptions

Runtime paths, concrete slot/process/PID values, IDs, timestamps, hashes, tokens,
environment values, and actual log contents are not wiki facts. Never bake them
into the generated Skill.

## Draft format

Use a compact reviewable draft:

```markdown
定位 Skill 2.0.0 草案
- Skill：diagnose-example@2.0.0
- capability：example
- module：EXAMPLE
- logparse product：example-product
- Router 摘要：用于定位合成示例故障

目标角色
| 标签 | 说明 | 是否必需 |
| --- | --- | --- |
| client | RPC 客户端进程 | 是 |
| server | RPC 服务端进程 | 是 |

允许 ContentType：application/gzip
自定义参数：order_id（必需）
```

## Generate deterministically

Prefer the standard-library API in
`scripts/generate_diagnosis_skill.py`:

```python
from generate_diagnosis_skill import build_spec_from_wiki, generate_diagnosis_skill

spec = build_spec_from_wiki(
    wiki_text,
    capability="service-takeover",
    summary="定位合成服务接管场景中的 RPC 超时",
    version="2.0.0",
    requires_logparse=True,
    logparse_product="payment-service",
    allowed_content_types=["application/gzip"],
)
generate_diagnosis_skill(spec, ".claude/skills")
```

Or run the CLI:

```bash
python3.12 -X utf8 \
  .claude/skills/wiki-to-diagnosis-skill/scripts/generate_diagnosis_skill.py \
  --wiki path/to/wiki.md \
  --output-root .claude/skills \
  --capability service-takeover \
  --summary '定位合成服务接管场景中的 RPC 超时' \
  --version 2.0.0 \
  --logparse-product payment-service \
  --allowed-content-type application/gzip
```

The CLI emits a deterministic product hash receipt. The generated product
contains exactly `SKILL.md` and `diagnosis-skill.json`; the latter is Canonical
JSON with the exact S04 field set. ContentTypes are validated with the S00
Canonical grammar, without normalization. The generator is idempotent for exact
bytes and refuses changed bytes under the same id/version.

## Validate

Run:

```bash
python3.12 -X utf8 \
  .claude/skills/wiki-to-diagnosis-skill/scripts/validate_generated_skill.py \
  .claude/skills/<skill-name>
```

Validation uses only the Python standard library. It checks the exact manifest,
Canonical JSON bytes, version, ContentTypes, required contract phrases, all four
DIAGNOSE result types, StateDelta boundaries, broker-only logparse use,
`LOGPARSE_RUN` reuse, and the Candidate plus unique USER_RESULT seam.

Report the generated path, Skill id/version, selected capability, confirmed
parameters, assumptions, product SHA-256, and validation result.

## Contract authority

Do not define local public DTOs, result types, error codes, broker errors, or
compatibility fields. Generated instructions must consume the installed S00
contract and current JSON Schemas. When S00 cannot express a required behavior,
stop that behavior and submit a contract change request instead of inventing a
private bridge.
