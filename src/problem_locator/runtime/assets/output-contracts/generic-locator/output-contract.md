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
