#!/usr/bin/env bash
set -euo pipefail

export PYTHONPYCACHEPREFIX=/tmp/attempt52-pycache
expected_product=ae47a1a63e6cf4849f83b0f9d49db608c1e93ebe1713f21d58c910990b0857a4
expected_skill_md=d4d1dd2085251a997eb32c325272377698443dced553c2f506b3bd44289630dc
expected_skill_json=b61a0d978bd4375ad9fc4975b6f197f224a8d328e9ef003c1700a2db536bbd1d

test ! -e /opt/e2e-skills
/opt/venvs/xiaodao/bin/python -X utf8 \
  /opt/src/xiaodao/.claude/skills/wiki-to-diagnosis-skill/scripts/generate_diagnosis_skill.py \
  --wiki /opt/src/xiaodao/tests/fixtures/components/logparse/wiki/service-takeover.md \
  --output-root /opt/e2e-skills \
  > /evidence/skill-generator-receipt.json
grep -Fq '"created":true' /evidence/skill-generator-receipt.json
grep -Fq '"product_sha256":"ae47a1a63e6cf4849f83b0f9d49db608c1e93ebe1713f21d58c910990b0857a4"' \
  /evidence/skill-generator-receipt.json
test "$(find /opt/e2e-skills/diagnose-service-takeover -type f | wc -l)" -eq 2
test "$(find /opt/e2e-skills/diagnose-service-takeover -type f -exec stat -c %h '{}' + | sort -u)" = '1'
test "$(sha256sum /opt/e2e-skills/diagnose-service-takeover/SKILL.md | awk '{print $1}')" = "$expected_skill_md"
test "$(sha256sum /opt/e2e-skills/diagnose-service-takeover/diagnosis-skill.json | awk '{print $1}')" = "$expected_skill_json"
cmp \
  /opt/e2e-skills/diagnose-service-takeover/SKILL.md \
  /opt/src/xiaodao/.claude/skills/diagnose-service-takeover/SKILL.md
cmp \
  /opt/e2e-skills/diagnose-service-takeover/diagnosis-skill.json \
  /opt/src/xiaodao/.claude/skills/diagnose-service-takeover/diagnosis-skill.json
/opt/venvs/xiaodao/bin/python \
  /opt/src/xiaodao/.claude/skills/wiki-to-diagnosis-skill/scripts/validate_generated_skill.py \
  /opt/e2e-skills/diagnose-service-takeover \
  > /evidence/skill-validator.txt

test ! -L /opt/e2e-skills
test -d /opt/e2e-skills
test "$(stat -c '%u:%g:%a' -- /opt/e2e-skills)" = '0:0:755'
test ! -L /opt/e2e-skills/diagnose-service-takeover
test -d /opt/e2e-skills/diagnose-service-takeover
test "$(stat -c '%u:%g:%a' -- /opt/e2e-skills/diagnose-service-takeover)" = '0:0:755'
test ! -L /opt/e2e-skills/diagnose-service-takeover/SKILL.md
test -f /opt/e2e-skills/diagnose-service-takeover/SKILL.md
test "$(stat -c '%u:%g:%a' -- /opt/e2e-skills/diagnose-service-takeover/SKILL.md)" = '0:0:644'
test ! -L /opt/e2e-skills/diagnose-service-takeover/diagnosis-skill.json
test -f /opt/e2e-skills/diagnose-service-takeover/diagnosis-skill.json
test "$(stat -c '%u:%g:%a' -- /opt/e2e-skills/diagnose-service-takeover/diagnosis-skill.json)" = '0:0:644'
test "$(sha256sum /opt/e2e-skills/diagnose-service-takeover/SKILL.md | awk '{print $1}')" = "$expected_skill_md"
test "$(sha256sum /opt/e2e-skills/diagnose-service-takeover/diagnosis-skill.json | awk '{print $1}')" = "$expected_skill_json"

/opt/venvs/xiaodao/bin/python /evidence/prepare_real_zip.py \
  > /evidence/zip-verification.txt

{
  printf 'skill_path=/opt/e2e-skills/diagnose-service-takeover\n'
  printf 'skill_generation_mode=live-generator-from-wiki\n'
  printf 'skill_generator_version=3.0.5\n'
  printf 'skill_capability=service-takeover\n'
  printf 'skill_summary=定位合成服务接管场景中的 RPC 超时\n'
  printf 'skill_logparse_product=compact\n'
  printf 'skill_content_types=application/gzip,application/zip,application/x-tar\n'
  printf 'skill_assumption=只使用合成服务名、合成订单号和非敏感日志。\n'
  printf 'skill_generated_bytes_equal_frozen_product=true\n'
  printf 'skill_validator=pass\n'
  printf 'skill_product_sha256=%s\n' "$expected_product"
  printf 'skill_md_sha256=%s\n' "$expected_skill_md"
  printf 'skill_json_sha256=%s\n' "$expected_skill_json"
  printf 'skill_file_nlink=1\n'
  printf 'skill_output_root_symlink=false\n'
  printf 'skill_output_root_owner=0:0\n'
  printf 'skill_output_root_mode=0755\n'
  printf 'skill_directory_symlink=false\n'
  printf 'skill_directory_owner=0:0\n'
  printf 'skill_directory_mode=0755\n'
  printf 'skill_md=regular,non-symlink,0:0,0644\n'
  printf 'skill_json=regular,non-symlink,0:0,0644\n'
  printf 'skill_file_hashes_reverified_after_permission_assertions=true\n'
  printf 'zip_base64_source_sha256=9161aa8ede7d6bc7cedefd9588b94dc5370112c44394911092dc6cd7da7c1f9e\n'
  printf 'zip_ascii_whitespace_removed=true\n'
  printf 'zip_strict_base64_validation=pass\n'
  printf 'zip_size=2367\n'
  printf 'zip_sha256=194f69fecd8dc8d40d1aedeb6fc25d2b7b4922b176be2b15be73ffe386cc5064\n'
  printf 'zip_member_count=4\n'
  printf 'zip_testzip=pass\n'
} > /evidence/skill-and-zip-verification.txt
