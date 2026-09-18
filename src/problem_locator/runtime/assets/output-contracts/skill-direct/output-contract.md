# Skill 报告输出合同

直接在最终响应中输出状态包络和 Markdown 报告，不要调用 Write 或创建草稿。

首行必须是以下两种之一：

`<<<SKILL_DIAGNOSIS_RESULT_V1:RESOLVED>>>`

`<<<SKILL_DIAGNOSIS_RESULT_V1:UNRESOLVED>>>`

首行换行后的全部内容就是交付给用户的报告正文，最长 65536 个 UTF-8 字节。不要在包络前增加说明，不要再用代码围栏包住整份响应。

按 Skill 的判断选择状态：Skill 已得出定位结论时使用 RESOLVED；Skill 自身尚无结论时使用 UNRESOLVED。正文沿用 Skill 要求的报告格式，保留其结论、理由、限制和建议。框架不额外规定证据数量、marker、身份词或逐行引用门槛，不要求输出证据核验 JSON，也不会把结果降级成 PARTIAL 或补写“证据不足”。
