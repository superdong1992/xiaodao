#!/usr/bin/env bash
set -euo pipefail

export HOME=/root
export PATH=/opt/venvs/xiaodao/bin:/opt/venvs/logparse/bin:/usr/local/bin:/usr/bin:/bin
export PYTHONNOUSERSITE=1
export PYTHONPYCACHEPREFIX=/tmp/attempt52-pycache
export UV_PYTHON_INSTALL_DIR=/opt/uv-python
export S08_NATIVE_STARTUP_GATE=linux
export SKILL_DIR=/opt/e2e-skills
export LOGPARSE_REPO=/opt/src/logparse
export LOGPARSE_CONFIG_PATH=/opt/src/logparse/config.yaml
export LOGPARSE_PYTHON=/opt/venvs/logparse/bin/python
export CLAUDE_COMMAND='/usr/bin/timeout --foreground --signal=TERM --kill-after=5s 240s /usr/local/bin/claude -p --no-chrome --no-session-persistence --dangerously-skip-permissions --tools Bash,Read,Write,Skill --allowedTools Skill(logparse-diagnose) --setting-sources user --settings /run/plagent-claude/settings.json --model haiku --effort low --max-budget-usd 3.00'

cd /opt/src/xiaodao
patch_sha=$(awk 'NR == 1 && $2 == "/evidence/source-input.patch" {print $1}' /evidence/source-input.patch.sha256)
test -n "$patch_sha"
case "${1:-}" in
  preclean)
    git -c core.autocrlf=false diff --name-only | LC_ALL=C sort > /tmp/attempt52-patch-files-preclean.txt
    cmp /tmp/attempt52-patch-files-preclean.txt /evidence/source.patch.files.txt
    test -z "$(git ls-files --others --exclude-standard)"
    test -z "$(git -C /opt/src/logparse status --porcelain --untracked-files=all)"
    test -z "$(git -C /opt/src/problem-locator-mcp status --porcelain --untracked-files=all)"
    test -z "$(find /opt/src/xiaodao /opt/src/logparse /opt/src/problem-locator-mcp \
      \( -type d -name __pycache__ -o -type f -name '*.pyc' \) -print -quit)"
    git -c core.autocrlf=false diff --binary --no-ext-diff > /tmp/attempt52-pre-pytest.patch
    test "$(sha256sum /tmp/attempt52-pre-pytest.patch | awk '{print $1}')" = "$patch_sha"
    cmp /tmp/attempt52-pre-pytest.patch /evidence/source.patch
    printf 'pre_pytest_sha256=%s\n' "$patch_sha" >> /evidence/patch-rehash-evidence.txt
    {
      printf 'xiaodao_expected_patch_only=true\n'
      printf 'xiaodao_untracked=none\n'
      printf 'logparse_tree=clean\n'
      printf 'problem_locator_mcp_tree=clean\n'
      printf 'source___pycache__=none\n'
      printf 'source_pyc=none\n'
      printf 'pycache_prefix=/tmp/attempt52-pycache\n'
    } > /evidence/pre-pytest-source-cleanliness.txt
    ;;
  target)
    python -m pytest -q \
      tests/unit/application/test_external_commands.py::test_submit_supplement_accepts_canonical_fact_order_for_multiple_inputs \
      tests/unit/integrations/test_generator_v2.py \
      tests/unit/integrations/test_logparse_outputs.py \
      tests/unit/integrations/test_logparse_primitives.py::test_git_inventory_trusts_only_the_exact_configured_repository \
      tests/unit/integrations/test_logparse_primitives.py::test_git_inventory_ignores_ambient_repository_and_config_redirection \
      tests/unit/integrations/test_logparse_primitives.py::test_git_inventory_rejects_a_safe_directory_wildcard_path \
      tests/unit/interfaces/test_mcp_server.py \
      tests/unit/runtime/test_catalog.py \
      --junitxml=/evidence/01-target-regression.xml
    ;;
  full)
    python -m pytest -q --junitxml=/evidence/02-full-suite.xml
    ;;
  post)
    python -m compileall -q src tests
    UV_PROJECT_ENVIRONMENT=/opt/venvs/xiaodao uv lock --check
    uv pip check --python /opt/venvs/xiaodao/bin/python
    uv pip check --python /opt/venvs/logparse/bin/python
    git -c core.autocrlf=false diff --check
    git -c core.autocrlf=false diff --binary --no-ext-diff > /tmp/attempt52-current.patch
    test "$(sha256sum /tmp/attempt52-current.patch | awk '{print $1}')" = "$patch_sha"
    cmp /tmp/attempt52-current.patch /evidence/source.patch
    install -m 0644 /tmp/attempt52-current.patch /evidence/host-current.patch
    git -c core.autocrlf=false diff --name-only | LC_ALL=C sort > /tmp/attempt52-patch-files.txt
    cmp /tmp/attempt52-patch-files.txt /evidence/source.patch.files.txt
    printf 'post_pytest_sha256=%s\n' "$patch_sha" >> /evidence/patch-rehash-evidence.txt
    test -z "$(git -C /opt/src/logparse status --porcelain --untracked-files=all)"
    test -z "$(git -C /opt/src/problem-locator-mcp status --porcelain --untracked-files=all)"
    test "$(git -C /opt/src/problem-locator-mcp remote get-url origin)" = \
      'https://github.com/superdong1992/problem-locator-mcp.git'
    test -z "$(find /var/lib/problem-locator -mindepth 1 -print -quit)"
    {
      printf 'compileall=pass\n'
      printf 'pycache_prefix=/tmp/attempt52-pycache\n'
      printf 'uv_lock_check=pass\n'
      printf 'xiaodao_pip_check=pass\n'
      printf 'logparse_pip_check=pass\n'
      printf 'git_diff_check=pass\n'
      printf 'source_patch_cmp=pass\n'
      printf 'source_patch_sha256=%s\n' "$patch_sha"
      printf 'logparse_tree=clean\n'
      printf 'problem_locator_mcp_tree=clean\n'
      printf 'data_root_still_empty=true\n'
    } > /evidence/pre-gates-post-verification.txt
    {
      printf 'service_home=/run/plagent-claude\n'
      printf 'service_path=%s\n' "$PATH"
      printf 'problem_locator_logparse=%s\n' "$(command -v problem-locator-logparse)"
      printf 'problem_locator_logparse_commands=parse-targets,target-logs\n'
      printf 'claude=%s\n' "$(command -v claude)"
      printf 'claude_command_uses_absolute_path=true\n'
      printf 'service_tools=Bash,Read,Write,Skill(logparse-diagnose)\n'
      printf 'service_allowed_tools=not-set\n'
      printf 'service_allowed_tools_reason=canonical JSON and atomic replacement may require python, mv, and sync through Bash; isolation is enforced by the container, tmpfs, prompt whitelist, and secret scan\n'
    } > /evidence/service-path-verification.txt
    test "$(command -v problem-locator-logparse)" = '/opt/venvs/xiaodao/bin/problem-locator-logparse'
    test "$(command -v claude)" = '/usr/local/bin/claude'
    problem-locator-logparse --help > /tmp/problem-locator-logparse-help.txt
    grep -Fq 'parse-targets' /tmp/problem-locator-logparse-help.txt
    grep -Fq 'target-logs' /tmp/problem-locator-logparse-help.txt
    ;;
  *)
    printf 'usage: %s {preclean|target|full|post}\n' "$0" >&2
    exit 2
    ;;
esac
