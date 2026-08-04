# Diagnosis Wiki 模板

普通 Wiki 章节可自由组织；机器生成只读取且要求恰好一个
`## GenerationSpec v2` JSON fence。下列是无日志人工排查的最小完整示例。

## GenerationSpec v2

```json
{
  "schema_version": 2,
  "generator_version": "3.0.4",
  "id": "diagnose-manual-triage",
  "version": "3.0.4",
  "capability": "manual-triage",
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
      }
    }
  ],
  "logparse_plan": null,
  "time_characteristics": [],
  "analysis_steps": ["复核现象与范围。"],
  "judgement_rules": ["证据不足时明确保留缺口。"],
  "output_requirements": ["给出可由 Evidence 支持的候选结论。"],
  "assumptions": [],
  "requires_logparse": false
}
```

Logparse Skill 可额外提供 `logparse_product`；仅非默认产品填写该字段。完整字段和约束
见 [generated-skill-contract.md](generated-skill-contract.md)。
