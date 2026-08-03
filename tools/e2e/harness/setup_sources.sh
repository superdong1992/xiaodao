#!/usr/bin/env bash
set -euo pipefail

xiaodao_base=c31cc03848155d03b9a35776555e413f26b264ad
logparse_commit=a233b500d9c99e6815d1ffd82cb4ca55bbfe657a
mcp_commit=97d0446580f49e7b1add1c5fc6d6a41c97884884
patch_source=/evidence/source-input.patch
patch_sha=$(awk 'NR == 1 && $2 == "/evidence/source-input.patch" {print $1}' /evidence/source-input.patch.sha256)
test -n "$patch_sha"

test "$(sha256sum "$patch_source" | awk '{print $1}')" = "$patch_sha"
install -m 0644 "$patch_source" /evidence/source.patch
test "$(sha256sum /evidence/source.patch | awk '{print $1}')" = "$patch_sha"

mkdir -p /opt/src
git -c core.autocrlf=false clone --no-hardlinks /source/xiaodao /opt/src/xiaodao
git -C /opt/src/xiaodao config core.autocrlf false
git -C /opt/src/xiaodao checkout --detach "$xiaodao_base"
test "$(git -C /opt/src/xiaodao rev-parse HEAD)" = "$xiaodao_base"
test -z "$(git -C /opt/src/xiaodao status --porcelain --untracked-files=all)"
git -C /opt/src/xiaodao apply --check /evidence/source.patch
git -C /opt/src/xiaodao apply /evidence/source.patch
git -C /opt/src/xiaodao add -N -- \
  tests/e2e/test_real_diagnose_agent_contract_gate.py \
  tests/e2e/test_real_route_agent_contract_gate.py
git -C /opt/src/xiaodao -c core.autocrlf=false diff --binary --no-ext-diff \
  > /tmp/e2e-after-apply.patch
test "$(sha256sum /tmp/e2e-after-apply.patch | awk '{print $1}')" = "$patch_sha"
cmp /tmp/e2e-after-apply.patch /evidence/source.patch
{
  printf 'template=git -c core.autocrlf=false diff --binary --no-ext-diff\n'
  printf 'frozen_source_patch_sha256=%s\n' "$patch_sha"
  printf 'after_apply_sha256=%s\n' "$patch_sha"
} > /evidence/patch-rehash-evidence.txt

git clone --no-hardlinks /source/logparse /opt/src/logparse
git -C /opt/src/logparse checkout --detach "$logparse_commit"
git -C /opt/src/logparse config core.autocrlf false
test "$(git -C /opt/src/logparse rev-parse HEAD)" = "$logparse_commit"
test -z "$(git -C /opt/src/logparse status --porcelain --untracked-files=all)"

git clone --no-hardlinks /source/problem-locator-mcp /opt/src/problem-locator-mcp
git -C /opt/src/problem-locator-mcp checkout --detach "$mcp_commit"
git -C /opt/src/problem-locator-mcp config core.autocrlf false
git -C /opt/src/problem-locator-mcp remote set-url origin https://github.com/superdong1992/problem-locator-mcp.git
test "$(git -C /opt/src/problem-locator-mcp rev-parse HEAD)" = "$mcp_commit"
test "$(git -C /opt/src/problem-locator-mcp remote get-url origin)" = 'https://github.com/superdong1992/problem-locator-mcp.git'
test -z "$(git -C /opt/src/problem-locator-mcp status --porcelain --untracked-files=all)"

{
  printf 'ubuntu_image=%s\n' 'ubuntu@sha256:3131b4cc82a783df6c9df078f86e01819a13594b865c2cad47bd1bca2b7063bb'
  printf 'xiaodao_base=%s\n' "$xiaodao_base"
  printf 'logparse=%s\n' "$logparse_commit"
  printf 'problem_locator_mcp=%s\n' "$mcp_commit"
  printf 'problem_locator_mcp_origin=%s\n' "$(git -C /opt/src/problem-locator-mcp remote get-url origin)"
  printf 'problem_locator_mcp_tree=clean\n'
  printf 'source_patch_sha256=%s\n' "$patch_sha"
  printf 'logparse_git_inventory_trust=command-scoped-exact-configured-repository\n'
  printf 'system_gitconfig_safe_directory_workaround=false\n'
  printf 'uv=0.11.32\n'
} > /evidence/source-pins.txt

git -C /opt/src/xiaodao diff --name-only | LC_ALL=C sort > /evidence/source.patch.files.txt
sha256sum /evidence/source.patch > /evidence/source.patch.sha256
