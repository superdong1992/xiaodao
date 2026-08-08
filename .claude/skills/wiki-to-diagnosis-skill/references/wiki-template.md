# Diagnosis Wiki 模板

普通 Wiki 章节可自由组织；机器生成只读取且要求恰好一个
`## GenerationSpec v4` JSON fence。下列是无日志人工排查的最小完整示例。

## GenerationSpec v4

```json
{
  "schema_version": 4,
  "generator_version": "4.0.0",
  "id": "diagnose-manual-triage",
  "version": "4.0.0",
  "capability": "manual-triage",
  "deployment_scope": "PRODUCTION",
  "summary": "根据用户提供的现象和复现步骤执行人工定位",
  "chinese_title": "人工故障定位",
  "module_name": null,
  "problem_scope": "不依赖日志解析，根据结构化事实和既有 Evidence 缩小故障范围。",
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
    "schema_version": 1,
    "event_extractors": [],
    "rules": [
      {
        "id": "manual_causal_assessment",
        "kind": "SEMANTIC_CAUSALITY",
        "description": "Specialist 和 Reviewer 必须独立判断证据是否支持根因。",
        "depends_on": [],
        "remediation_requirements": [],
        "parameters": {
          "assertion": "现有事实和 Evidence 足以支持候选根因。",
          "evidence_events": []
        }
      }
    ]
  },
  "time_characteristics": [],
  "analysis_steps": ["复核现象与范围。"],
  "judgement_rules": ["证据不足时明确保留缺口。"],
  "output_requirements": ["给出可由 Evidence 支持的候选结论。"],
  "assumptions": [],
  "requires_logparse": false
}
```

Logparse Skill 可额外提供 `logparse_product`；仅非默认产品填写该字段。其归档 requirement
只填写 `min_count` 和 `max_count`，归档 Content-Type 由生成器固定注入，不向用户询问。
完整字段和约束见 [generated-skill-contract.md](generated-skill-contract.md)。
