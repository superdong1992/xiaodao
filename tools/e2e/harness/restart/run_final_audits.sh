#!/bin/sh
set -eu

python=/opt/venvs/xiaodao/bin/python
audit_root=/audit-input/state-audit
test -x "$python"
test -d "$audit_root"
test -s /audit-input/state-export.before.json
test -s /evidence/state-export.after.json
test -s /audit-input/journey-authoritative-summary.json
test -s /audit-input/restart-authoritative-summary.json
test -s /audit-input/diagnosis-result.before.json
test -s /audit-input/diagnosis-result.after.json
test ! -e /evidence/final-state-audit.json
test ! -e /evidence/http-artifact-audit.json

cd "$audit_root"
sha256sum --check --strict template-manifest.sha256
"$python" -I -c 'from pathlib import Path
for path in (Path("audit_state_and_result.py"), Path("audit_http_capture.py")):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")'
"$python" -I audit_state_and_result.py --help >/dev/null
"$python" -I audit_http_capture.py --help >/dev/null

"$python" -I audit_state_and_result.py \
  --before-export /audit-input/state-export.before.json \
  --after-export /evidence/state-export.after.json \
  --before-validation /audit-input/validate-state.before.json \
  --after-validation /evidence/validate-state.after.json \
  --journey-summary /audit-input/journey-authoritative-summary.json \
  --restart-summary /audit-input/restart-authoritative-summary.json \
  --before-result /audit-input/diagnosis-result.before.json \
  --after-result /audit-input/diagnosis-result.after.json \
  --user-result-schema /opt/src/xiaodao/schemas/v1/user-result.schema.json \
  --output /evidence/final-state-audit.json

"$python" -I audit_http_capture.py \
  --before-headers /audit-input/diagnosis-result.before.headers \
  --after-headers /audit-input/diagnosis-result.after.headers \
  --internal-headers /audit-input/internal-logparse.after.headers \
  --before-meta /audit-input/diagnosis-result.before.meta.json \
  --after-meta /audit-input/diagnosis-result.after.meta.json \
  --internal-meta /audit-input/internal-logparse.after.meta.json \
  --before-result /audit-input/diagnosis-result.before.json \
  --after-result /audit-input/diagnosis-result.after.json \
  --internal-body /audit-input/internal-logparse.after.body.json \
  --journey-summary /audit-input/journey-authoritative-summary.json \
  --restart-summary /audit-input/restart-authoritative-summary.json \
  --state-export /evidence/state-export.after.json \
  --output /evidence/http-artifact-audit.json

grep -Fq '"status":"PASS"' /evidence/final-state-audit.json
grep -Fq '"status":"PASS"' /evidence/http-artifact-audit.json
printf '%s\n' \
  'state_audit=PASS' \
  'http_artifact_audit=PASS' \
  'audit_template_manifest=PASS' \
  'audit_python_syntax_and_import=PASS' \
  > /evidence/final-audit-gate.txt
