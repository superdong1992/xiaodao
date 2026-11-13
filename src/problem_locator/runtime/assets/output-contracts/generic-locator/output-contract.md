# Generic locator output contract V2

Write exactly one result file at `output/generic_diagnosis_result.md`. Do not also
write the legacy `output/generic_diagnosis_result.txt` file.

The first bytes of the file are exactly one of these two ASCII status marker lines,
using one LF byte and no Markdown fence around the complete file:

```text
<<<GENERIC_DIAGNOSIS_RESULT_V2:RESOLVED>>>
```

```text
<<<GENERIC_DIAGNOSIS_RESULT_V2:UNRESOLVED>>>
```

Every byte after that first LF is the complete user-facing Markdown report. The
report body must be non-empty, not consist only of Unicode whitespace, use strict
UTF-8 without a byte-order mark (BOM), and contain at most 65536 UTF-8 bytes.
Preserve the report body exactly:
do not summarize it, split it into framework fields, trim it, normalize Unicode or
line endings, or add framework prose. Normal Markdown is allowed inside the body,
including headings, lists, tables, links, inline code, fenced code blocks, CRLF line
endings, and text that merely resembles a framework marker.

For `UNRESOLVED`, the Markdown report must state the leading hypotheses and the
missing information that prevents confirmation. Do not return the report only as
chat text, stdout, or stderr; the V2 result file is the sole authoritative result.

以上是推荐的输出格式。服务端接受状态行的 BOM、空白、大小写和换行差异，以及包住状态行与整份报告的 Markdown 围栏。缺少状态行或状态无法识别时，非空报告按 UNRESOLVED 原文交付，不猜测诊断是否完成，不因此报错或记录异常。只移除明确的展示包裹和开头 BOM，正文中的普通代码块、标记示例及其他内容保持原样。旧 V1 报告也允许正文包含代码块或标记示例。

若新旧两个结果文件同时存在，优先读取 V2 Markdown 文件；V2 文件为空、超限或无法安全读取时仍失败，不改用旧文件掩盖问题。文件路径、文件身份、UTF-8、正文非空及 65536 字节上限保持不变，展示包裹额外允许最多 1024 字节。
