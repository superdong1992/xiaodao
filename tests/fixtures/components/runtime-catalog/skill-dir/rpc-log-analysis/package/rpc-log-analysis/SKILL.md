---
name: rpc-log-analysis
description: Diagnose RPC timeout evidence from one frozen Logparse target set.
---

# RPC timeout log analysis

Read `request.json` and `methods.json`, scan every `target_logs` file for all positive markers, then use the matching method cards. Do not invoke Logparse during diagnosis. Preserve full `sources` and same-source `identity_tokens`.
