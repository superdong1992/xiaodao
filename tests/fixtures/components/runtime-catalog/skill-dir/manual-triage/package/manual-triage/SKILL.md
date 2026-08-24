---
name: manual-triage
description: Diagnose a service incident from frozen structured evidence without Logparse.
---

# Manual service triage

Read `request.json` and `methods.json`, then scan every declared `target_logs` source for all positive markers. Do not invoke Logparse. Preserve complete `sources` and same-source `identity_tokens` in every finding.
