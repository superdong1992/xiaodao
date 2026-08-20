# Controlled checkpoint 04: submit the compact GenerationBlueprint IR

- checkpoint_schema_version: 1
- checkpoint_id: write_now
- control_only: true
- must_not_enter_generation_spec: true

This checkpoint contains no source facts, business semantics, defaults, field values, or output content. Do not copy,
summarize, encode, cite, or otherwise place this file, its path, its id, or its instructions in any business field.

The compact blueprint, section 9.1, and section 9.2 stages have passed. The next `StructuredOutput` tool input is the
first and only materialization of the `GenerationBlueprint` v1 IR. It is not the expanded GenerationSpec. Do not place
the 144 ordered-interval family rule objects or the three generated family path objects in the IR. If any precondition
is false, stop without submitting.

The IR root has exactly four required keys: `schema_version`, `compiler`, `spec`, and `verification`.

- `schema_version` is integer `1`.
- `compiler` has exactly `id`=`generation-blueprint-ordered-interval` and `version`=`1.0.0`.
- `spec` contains the 19 required final GenerationSpec fields other than `verification_contract`:
  `schema_version`, `generator_version`, `id`, `version`, `capability`, `deployment_scope`, `summary`, `chinese_title`,
  `module_name`, `problem_scope`, `roles`, `requirements`, `logparse_plan`, `time_characteristics`, `analysis_steps`,
  `judgement_rules`, `output_requirements`, `assumptions`, `requires_logparse`. Its constants are
  `schema_version`=6, `generator_version`=`6.0.0`, and `requires_logparse`=true. `logparse_product` is its only optional
  key and must appear only when independently supported by the source. Thus `spec` has 19 or 20 keys; the expanded
  GenerationSpec has 20 required keys plus that same optional key.
- `verification` has exactly `schema_version`, `observation_policies`, `event_extractors`, `literal_rule_segments`,
  `literal_terminal_segments`, `ordered_interval_family`, and `expected_counts`.

Use this provider-equivalent typed frame. Every ALL_CAPS token is metasyntax that must be replaced from the compact
blueprint. No token, angle bracket, explanatory label, checkpoint text, or control sentinel may enter the tool input:

```text
{
  schema_version: 1,
  compiler: { id: "generation-blueprint-ordered-interval", version: "1.0.0" },
  spec: {
    schema_version: 6, generator_version: "6.0.0",
    id: <ID>, version: <VERSION>, capability: <CAPABILITY>, deployment_scope: <DEPLOYMENT_SCOPE>,
    summary: <SUMMARY>, chinese_title: <CHINESE_TITLE>, module_name: <MODULE_NAME>, problem_scope: <PROBLEM_SCOPE>,
    roles: <EXACTLY_2_ROLE_OBJECTS>, requirements: <EXACTLY_5_REQUIREMENT_OBJECTS>,
    logparse_plan: { anchors: <EXACTLY_2_ANCHOR_OBJECTS> },
    time_characteristics: <EXACTLY_4_STRINGS>, analysis_steps: <EXACTLY_5_STRINGS>,
    judgement_rules: <EXACTLY_6_STRINGS>, output_requirements: <EXACTLY_5_STRINGS>,
    assumptions: <EXACTLY_3_STRINGS>, requires_logparse: true,
    logparse_product: <SOURCE_SUPPORTED_NONEMPTY_STRING_IF_PRESENT>
  },
  verification: {
    schema_version: 2,
    observation_policies: <EXACTLY_2_POLICY_OBJECTS>,
    event_extractors: <EXACTLY_10_EXTRACTOR_OBJECTS>,
    literal_rule_segments: {
      prefix: <EXACTLY_7_FINAL_RULES_0_TO_6>,
      middle: <EXACTLY_9_FINAL_RULES_112_TO_120>,
      suffix: <EXACTLY_5_FINAL_RULES_160_TO_164>
    },
    literal_terminal_segments: {
      after_complete: <EXACTLY_2_FINAL_PATHS_1_TO_2>,
      after_families: <EXACTLY_4_FINAL_PATHS_5_TO_8>
    },
    ordered_interval_family: {
      kind: "ORDERED_INTERVAL", version: 1, namespace: <SAFE_RULE_NAMESPACE>,
      positions: <EXACTLY_5_POSITION_OBJECTS>,
      shared: <EXACT_SHARED_DEPENDENCY_OBJECT>,
      texts: <EXACT_CONTROLLED_TEXT_OBJECT>,
      names: { unattributed_semantic_suffix: <SAFE_RULE_SUFFIX> },
      terminal_paths: <EXACT_COMPLETE_UNATTRIBUTED_MIXED_PATH_METADATA>
    },
    expected_counts: {
      positions: 5, policies: 2, extractors: 10,
      prefix_rules: 7, mechanical_rules: 105, middle_rules: 9,
      semantic_rules: 39, suffix_rules: 5, total_rules: 165,
      family_terminal_paths: 3, literal_terminal_paths: 6, total_terminal_paths: 9
    }
  }
}
```

Every position object has exactly `ordinal`, `name`, `event`, `end_field`, `cost_field`, `queue_field`, and
`timeout_field`; ordinals are 1..5 in array order and all identifiers come from the author-confirmed matrix. `shared`
has exactly `call_event`, `call_timeout_field`, `call_present_rule_id`, `detail_event`, `detail_timeout_field`,
`detail_present_rule_id`, and exactly two `base_semantic_dependency_rule_ids`. `texts` has exactly these nonempty
controlled composition slots: `present_prefix`, `present_suffix`, `timeout_infix`, `timeout_suffix`, `core_prefix`,
`core_infix`, `core_suffix`, `serial_prefix`, `serial_infix`, `serial_suffix`, `interval_prefix`, `interval_infix`,
`interval_suffix`, `unattributed_assertion`, `overlap_assertion`, `full_assertion`, `gap_assertion`.
`terminal_paths` has exactly `complete`, `unattributed`, and `mixed`; each contains only `id` and
`resolution_status`, with statuses COMPLETE, PARTIAL, PARTIAL respectively.

The compiler inserts 105 mechanical family rules after `prefix`, then `middle`, then 39 semantic family rules, then
`suffix`, preserving the exact 7+105+9+39+5=165 order. It creates the complete family path first, inserts the two
`after_complete` literal paths, creates the unattributed and mixed family paths, then appends the four
`after_families` paths, preserving all nine paths' first-match order. Literal arrays must contain complete typed objects;
the ordered family must not contain a `rules` array or any explicit expanded rule object.

Your next and only tool call must be `StructuredOutput`, with this complete compact IR root plain object itself as the
tool input. Do not wrap it in another field or turn it into a JSON string. The CLI schema validates the protocol-parsed
IR before success. The trusted wrapper binds the IR input to the terminal IR, invokes the versioned deterministic
compiler in memory, runs the existing deep GenerationSpec loader and verification validator, then and only then owns
canonical encoding and create-only atomic output at `workspace/output/generation-spec.json`. The final output remains a
complete GenerationSpec v6 with 2 policies, 10 extractors, 165 rules, and 9 paths.

The IR canonical payload must be at most 48 KiB. `StructuredOutput` is not a schema-discovery or validation probe. Never
submit a zero-property root, partial IR, expanded 144-rule family, trial input, or probe input. The first invocation must
already be the complete schema-valid IR and is the only invocation permitted.

This legacy checkpoint filename does not authorize `Write`. Do not call `Write`, `Edit`, or `Bash`, and do not manually
serialize, create, or modify a file. Wait for this unique `StructuredOutput` result. If and only if it reports success,
emit one terminal assistant response whose complete content is exact ASCII `DONE`, with no quotes, Markdown, whitespace,
punctuation, or other text. `DONE` is control-only and must not enter the IR or generated output. If the tool reports an
error, stop immediately without repairing or retrying; never call `StructuredOutput` a second time. After `DONE`, emit
no text, tool call, or further turn.
