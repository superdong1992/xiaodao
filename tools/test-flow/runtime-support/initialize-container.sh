#!/bin/sh
# Runtime support shared by the first-party CrossJob adapters.
set -eu
umask 077

mode=${1:?fresh or restart mode required}
expected_xiaodao_snapshot=${2:?xiaodao source snapshot digest required}
expected_logparse=${3:?logparse commit required}
expected_mcp=${4:?mcp commit required}
receipt=${5:?absolute receipt path required}

case "$mode" in
  fresh|restart) ;;
  *) exit 64 ;;
esac
case "$receipt" in
  /evidence/stages/*/container-init.json) ;;
  *) exit 64 ;;
esac

test "$(id -u)" = 0
test -x /usr/local/bin/claude
test "$(/usr/local/bin/claude --version)" = '2.1.89 (Claude Code)'
test -x /opt/venvs/xiaodao/bin/python
test -x /opt/venvs/logparse/bin/python
test ! -e /opt/src
install -d -m 0755 -o 0 -g 0 /opt/src

source_git_config=/tmp/test-flow-source-gitconfig
test ! -e "$source_git_config"
git config --file "$source_git_config" --add safe.directory ''
git config --file "$source_git_config" --add safe.directory /source/logparse/.git
git config --file "$source_git_config" --add safe.directory /source/problem-locator-mcp/.git
chmod 0600 "$source_git_config"
test "$(GIT_CONFIG_GLOBAL="$source_git_config" git config --global --get-all safe.directory)" = "
/source/logparse/.git
/source/problem-locator-mcp/.git"

test -d /source/xiaodao
install -d -m 0755 /opt/src/xiaodao
cp -a /source/xiaodao/. /opt/src/xiaodao/
node /test-flow-runtime/verify-source-snapshot.mjs \
  --root /opt/src/xiaodao \
  --manifest /evidence/source/source-snapshot.json \
  --expected-digest "$expected_xiaodao_snapshot" \
  --normalize-modes-from-manifest >/dev/null

GIT_CONFIG_GLOBAL="$source_git_config" git -c core.autocrlf=false clone --no-hardlinks /source/logparse /opt/src/logparse >/dev/null
git -C /opt/src/logparse checkout --detach "$expected_logparse" >/dev/null
test "$(git -C /opt/src/logparse rev-parse HEAD)" = "$expected_logparse"
test -z "$(git -C /opt/src/logparse status --porcelain --untracked-files=all)"

GIT_CONFIG_GLOBAL="$source_git_config" git -c core.autocrlf=false clone --no-hardlinks /source/problem-locator-mcp /opt/src/problem-locator-mcp >/dev/null
git -C /opt/src/problem-locator-mcp checkout --detach "$expected_mcp" >/dev/null
test "$(git -C /opt/src/problem-locator-mcp rev-parse HEAD)" = "$expected_mcp"
test -z "$(git -C /opt/src/problem-locator-mcp status --porcelain --untracked-files=all)"
rm -f "$source_git_config"

UV_LINK_MODE=copy UV_NO_PROGRESS=1 uv pip install \
  --offline --no-deps --no-build-isolation --reinstall \
  --python /opt/venvs/xiaodao/bin/python \
  /opt/src/xiaodao >/dev/null
/opt/venvs/xiaodao/bin/python -I -c 'import problem_locator; assert problem_locator.__version__'
installed_assets=/opt/venvs/xiaodao/lib/python3.12/site-packages/problem_locator/runtime/assets
test -d "$installed_assets"
test -z "$(find "$installed_assets" -xdev -type l -print -quit)"
test -z "$(find "$installed_assets" -xdev -type f -links +1 -print -quit)"

test ! -e /opt/e2e-skills
/opt/venvs/xiaodao/bin/python -I \
  /opt/src/xiaodao/.claude/skills/wiki-to-diagnosis-skill/scripts/generate_diagnosis_skill.py \
  --wiki /opt/src/xiaodao/tests/fixtures/components/logparse/wiki/service-takeover.md \
  --output-root /opt/e2e-skills >/dev/null
/opt/venvs/xiaodao/bin/python -I \
  /opt/src/xiaodao/.claude/skills/wiki-to-diagnosis-skill/scripts/validate_generated_skill.py \
  /opt/e2e-skills/diagnose-service-takeover >/dev/null

if [ "$mode" = fresh ]; then
  test -z "$(find /var/lib/problem-locator -mindepth 1 -print -quit)"
  test ! -e /evidence/synthetic-rpc-service-takeover.zip
  /opt/venvs/xiaodao/bin/python -I /test-flow-runtime/prepare_real_zip.py >/dev/null
else
  test -n "$(find /var/lib/problem-locator -mindepth 1 -print -quit)"
  test -f /evidence/synthetic-rpc-service-takeover.zip
fi

/opt/venvs/xiaodao/bin/python -I /test-flow-runtime/prepare_claude_settings.py >/dev/null
test "$(stat -c %a /root/.claude/settings.json)" = 600

if ! getent group 10001 >/dev/null; then groupadd --gid 10001 plagent; fi
if ! getent passwd 10001 >/dev/null; then
  useradd --uid 10001 --gid 10001 --no-create-home --home-dir /run/plagent-claude --shell /bin/sh plagent
fi
test "$(id -u plagent)" = 10001
test "$(id -g plagent)" = 10001
chown 10001:10001 /run/plagent-claude
chmod 0700 /run/plagent-claude
install -d -m 0700 -o 10001 -g 10001 /run/plagent-claude/.claude /run/plagent-claude/.claude/skills
test -f /run/plagent-claude/.claude/skills/logparse-diagnose/SKILL.md
/opt/venvs/xiaodao/bin/python -I /test-flow-runtime/prepare_nonroot_settings.py create >/dev/null
chown 10001:10001 /run/plagent-claude/settings.json
/opt/venvs/xiaodao/bin/python -I /test-flow-runtime/prepare_nonroot_settings.py verify

chown 10001:10001 /var/lib/problem-locator
chmod 0700 /var/lib/problem-locator
chmod -R a+rX /opt/src /opt/e2e-skills /opt/venvs /opt/uv-python
chmod -R go-w /opt/src /opt/e2e-skills /opt/venvs /opt/uv-python
for tree in /opt/src /opt/e2e-skills /opt/venvs /opt/uv-python; do
  test -z "$(runuser -u plagent -- find "$tree" -xdev ! -readable -print -quit)"
  test -z "$(runuser -u plagent -- find "$tree" -xdev -type d ! -executable -print -quit)"
  test -z "$(runuser -u plagent -- find "$tree" -xdev -writable -print -quit)"
done
runuser -u plagent -- test -w /var/lib/problem-locator
runuser -u plagent -- test -r /run/plagent-claude/settings.json

mkdir -p "$(dirname "$receipt")"
test ! -e "$receipt"
cat > "$receipt" <<EOF
{"schema_version":1,"status":"PASS","mode":"$mode","xiaodao_snapshot_digest":"$expected_xiaodao_snapshot","logparse_commit":"$expected_logparse","mcp_commit":"$expected_mcp","claude_version":"2.1.89 (Claude Code)","service_uid":10001,"service_gid":10001,"settings_policy":"env-allowlist-only-no-hooks-v1","network_install":false,"uv_link_mode":"copy","installed_asset_hardlinks":0}
EOF
chmod 0600 "$receipt"
