# Diagnosis Wiki / GenerationSpec v5 模板

Wiki 作者可以自由写普通 Markdown，并用 `(# ... #)` 或 `（# ... #）` 给转换 Agent 写不进入
产物的旁注。下面 JSON 是转换 Agent 的中间产物最小示例，不是要求 Wiki 作者填写的正文格式。

## GenerationSpec v5

```json
{
  "schema_version": 5,
  "generator_version": "5.0.0",
  "id": "diagnose-manual-triage",
  "version": "5.0.0",
  "capability": "manual-triage",
  "deployment_scope": "PRODUCTION",
  "summary": "根据用户证据执行人工定位",
  "chinese_title": "人工故障定位",
  "module_name": null,
  "problem_scope": "不依赖 Logparse，依据固定事实和 Evidence 缩小范围。",
  "roles": [],
  "requirements": [
    {
      "name": "affected_component",
      "kind": "INPUT",
      "stage": "INITIAL",
      "fulfillment_source": "USER_FACT",
      "prompt": "请提供受影响组件。",
      "constraints": {
        "value_type": "STRING",
        "min_utf8_bytes": 1,
        "max_utf8_bytes": 256,
        "pattern": null,
        "allowed_values": []
      },
      "supplement_policy": "MISSING_ONLY"
    }
  ],
  "logparse_plan": null,
  "verification_contract": {
    "schema_version": 2,
    "observation_policies": [],
    "event_extractors": [],
    "rules": [
      {
        "id": "manual_causal_assessment",
        "kind": "SEMANTIC_CAUSALITY",
        "description": "Specialist 与 Reviewer 独立判断固定 Evidence 是否支持结论。",
        "depends_on": [],
        "remediation_requirements": [],
        "parameters": {
          "assertion": "固定 Evidence 足以支持候选结论。",
          "evidence_events": []
        }
      }
    ],
    "terminal_paths": [
      {
        "id": "complete",
        "resolution_status": "COMPLETE",
        "condition": {
          "any_of": [
            {"all_of": [{"rule_id": "manual_causal_assessment", "result": "PASS"}]}
          ]
        }
      },
      {
        "id": "none",
        "resolution_status": "NONE",
        "condition": {"any_of": [{"all_of": []}]}
      }
    ]
  },
  "time_characteristics": [],
  "analysis_steps": ["复核现象与固定 Evidence。"],
  "judgement_rules": ["证据不足时选择 NONE，不虚构结论。"],
  "output_requirements": ["给出 Evidence 支持的结构化结论。"],
  "assumptions": [],
  "requires_logparse": false
}
```

Logparse Skill 可额外提供非默认 `logparse_product`。完整字段约束见
[generated-skill-contract.md](generated-skill-contract.md)。
