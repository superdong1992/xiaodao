# Conversation Archive

- Work item: `{{WORK_ITEM_ID}}`
- Predecessor work item: `NONE|{{PREDECESSOR_CANONICAL_PATH_AND_DIGESTS}}`
- Created: `{{CREATED_AT}}`
- Purpose: Preserve user/assistant visible dialogue as recovery context and audit evidence.
- Authority: Evidence only; `design.md` and `goal.md` define approved behavior and execution.
- Mutation rule: Append-only. Never edit or delete an existing entry.

## Entries

<!--
Append entries in sequence. Preserve visible content between the markers unless redaction is required.

### Entry 0001
- Role: user|assistant
- Kind: message|commentary|question|options|final|correction
- Redaction: false|true
- Redaction reason: none|<reason without the sensitive value>

BEGIN VISIBLE CONTENT 0001
<exact visible content or [REDACTED:<type>]>
END VISIBLE CONTENT 0001
-->
