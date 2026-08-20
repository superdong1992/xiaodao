---
name: adapt-lan-generic-locator-v2
description: Adapt an existing private LAN generic-diagnosis Skill to support Problem Locator Generic V2 framework output while preserving its native direct mode and V1 fallback. Use when preparing or locally auditing that private Skill for framework integration; do not use it to diagnose a user problem.
---

# Adapt LAN Generic Locator V2

Work only on the private Skill directory the user explicitly places in scope. Preserve its native diagnosis workflow and business references; add only the output-mode routing required by Problem Locator.

Before editing, read [the framework-mode contract](references/framework-mode.md). Apply its marked adapter block once inside the target `SKILL.md`, adapting surrounding prose only where needed to fit the Skill's existing workflow. Do not copy private business instructions or example reports into this repository.

After editing, run the local validator with an explicitly chosen non-sensitive
version label:

```text
python <this-skill-root>/scripts/verify_generic_locator_v2.py validate-skill --skill-root <absolute-private-skill-root> --skill-version <non-sensitive-version>
```

The validator checks structure and hashes the target locally. It does not execute
the private Skill or call a model. For an authorized LAN A/B run, follow the
content-free receipt procedure in the reference. The two model calls are not
expected to be byte-identical. The script records their hashes and the operator's
explicit semantic verdict; it never infers semantic equivalence from headings or
other report text. Exact comparison belongs only to the repository's deterministic
TEST_ONLY oracle driver, not to a private model-backed Skill.

Do not upload the Skill, reports, prompt, tool output, paths, identity manifests,
or receipt. Receipts contain only the declared version, tree/input/report/identity
manifest hashes and sizes, controlled status values, and `content_included=false`.
Stop if the target cannot keep native, V1, and V2 modes distinct or if the user
has not authorized editing that directory.
