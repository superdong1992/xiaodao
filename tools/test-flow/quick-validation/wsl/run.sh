#!/usr/bin/env bash
set -euo pipefail

tool_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$tool_root/../../../.." && pwd)

if [[ ${1:-} == "--inside-container" ]]; then
  shift
  flow_args=(
    --track release
    --goal release.evidence-v2-certification
    --client linux
    --scenario multiple-rpc-timeouts
    --resume fresh
    --repo-root "$repo_root"
    --evidence-root /evidence
    --claude-entry /opt/claude-cache/package/cli.js
    --claude-settings /run/secrets/claude-settings.json
    --codex-entry /usr/bin/codex
    --codex-auth /run/secrets/codex-auth.json
    --cache-root /cache
    --allow-codex-posthoc-budget
    "$@"
  )
  test "$(id -u):$(id -g)" = "0:0" || { printf 'CONTAINER_ROOT_REQUIRED\n' >&2; exit 3; }
  for argument in "$@"; do
    if [[ $argument == "--plan-only" ]]; then
      exec bash "$repo_root/tools/test-flow/run.sh" "${flow_args[@]}"
    fi
  done
  [[ ${TEST_FLOW_HOST_UID:-} =~ ^[0-9]+$ ]] || { printf 'HOST_UID_INVALID\n' >&2; exit 3; }
  [[ ${TEST_FLOW_HOST_GID:-} =~ ^[0-9]+$ ]] || { printf 'HOST_GID_INVALID\n' >&2; exit 3; }
  flow_output=$(mktemp /tmp/pltf-ev2-central-output.XXXXXX)
  trap 'rm -f -- "$flow_output"' EXIT
  set +e
  bash "$repo_root/tools/test-flow/run.sh" "${flow_args[@]}" | tee "$flow_output"
  flow_status=${PIPESTATUS[0]}
  set -e
  attempt_root=$(node -e 'const fs = require("node:fs"); try { const value = JSON.parse(fs.readFileSync(process.argv[1], "utf8")); process.stdout.write(typeof value.attempt_root === "string" ? value.attempt_root : ""); } catch {}' "$flow_output")
  if [[ -n $attempt_root ]]; then
    attempt_name=${attempt_root#/evidence/}
    case "$attempt_root" in /evidence/run-*) ;; *) printf 'ATTEMPT_ROOT_INVALID:%s\n' "$attempt_root" >&2; exit 3 ;; esac
    case "$attempt_name" in ""|*/*) printf 'ATTEMPT_ROOT_INVALID:%s\n' "$attempt_root" >&2; exit 3 ;; esac
    test -d "$attempt_root" && test -f "$attempt_root/verdict.json" || { printf 'AUTHORITATIVE_ATTEMPT_MISSING:%s\n' "$attempt_root" >&2; exit 3; }
    chown -R -- "$TEST_FLOW_HOST_UID:$TEST_FLOW_HOST_GID" "$attempt_root" || { printf 'ATTEMPT_CHOWN_FAILED:%s\n' "$attempt_root" >&2; exit 3; }
  elif ((flow_status == 0)); then
    printf 'ATTEMPT_ROOT_MISSING\n' >&2
    exit 3
  fi
  exit "$flow_status"
fi

cache_root=""
evidence_root=""
codex_auth=""
claude_settings=""
seal=""
plan_only=false
retry_args=()

while (($#)); do
  case "$1" in
    --cache-root) cache_root=$2; shift 2 ;;
    --evidence-root) evidence_root=$2; shift 2 ;;
    --codex-auth) codex_auth=$2; shift 2 ;;
    --claude-settings) claude_settings=$2; shift 2 ;;
    --image-seal) seal=$2; shift 2 ;;
    --reason|--hypothesis|--expected-evidence)
      (($# >= 2)) || { printf 'ARGUMENT_VALUE_REQUIRED:%s\n' "$1" >&2; exit 2; }
      retry_args+=("$1" "$2")
      shift 2
      ;;
    --plan-only) plan_only=true; shift ;;
    *) printf 'UNKNOWN_WRAPPER_ARGUMENT:%s\n' "$1" >&2; exit 2 ;;
  esac
done

test -n "$cache_root" || { printf 'CACHE_ROOT_REQUIRED\n' >&2; exit 2; }
test -n "$evidence_root" || { printf 'EVIDENCE_ROOT_REQUIRED\n' >&2; exit 2; }
test -n "$codex_auth" || { printf 'CODEX_AUTH_REQUIRED\n' >&2; exit 2; }
test -n "$claude_settings" || { printf 'CLAUDE_SETTINGS_REQUIRED\n' >&2; exit 2; }
case "$cache_root:$evidence_root:$codex_auth:$claude_settings" in
  /*:/*:/*:/*) ;;
  *) printf 'ABSOLUTE_PATHS_REQUIRED\n' >&2; exit 2 ;;
esac
case "$(uname -r)" in *[Mm]icrosoft*WSL2*|*[Mm]icrosoft-standard-WSL2*) ;; *) printf 'WSL2_REQUIRED\n' >&2; exit 2;; esac
test "$(. /etc/os-release && printf '%s' "$ID:$VERSION_ID")" = "ubuntu:22.04" || { printf 'UBUNTU_2204_HOST_REQUIRED\n' >&2; exit 2; }
test "$(stat -f -c %T "$repo_root")" = "ext2/ext3" || { printf 'EXT4_REPO_REQUIRED\n' >&2; exit 2; }
command -v docker >/dev/null || { printf 'DOCKER_REQUIRED\n' >&2; exit 2; }
test "$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')" = "linux/amd64" || { printf 'DOCKER_LINUX_AMD64_REQUIRED\n' >&2; exit 2; }
test -d "$cache_root" || { printf 'CACHE_ROOT_MISSING\n' >&2; exit 2; }
test -f "$codex_auth" || { printf 'CODEX_AUTH_MISSING\n' >&2; exit 2; }
test -f "$claude_settings" || { printf 'CLAUDE_SETTINGS_MISSING\n' >&2; exit 2; }

seal=${seal:-"$cache_root/quick-validation/ubuntu2204-central/image-seal.json"}
test -f "$seal" || { printf 'IMAGE_SEAL_MISSING\n' >&2; exit 2; }
image_id=$(sed -n 's/.*"image_id":"\([^"]*\)".*/\1/p' "$seal")
test -n "$image_id" || { printf 'IMAGE_SEAL_INVALID\n' >&2; exit 2; }
test "$(docker image inspect "$image_id" --format '{{.Id}}')" = "$image_id" || { printf 'IMAGE_ID_DRIFT\n' >&2; exit 2; }
test "$(docker image inspect "$image_id" --format '{{index .Config.Labels "problem-locator.quick.container"}}')" = "ubuntu22.04-central-v1" || { printf 'IMAGE_PROFILE_INVALID\n' >&2; exit 2; }

mkdir -p -m 700 "$evidence_root"
host_uid=$(id -u)
host_gid=$(id -g)
container_name="pltf-ev2-release-$(date -u +%Y%m%dT%H%M%SZ)-$$"
preflight_name="${container_name}-preflight"
launcher_output=$(mktemp "${TMPDIR:-/tmp}/pltf-ev2-release-output.XXXXXX")

cleanup() {
  local status=$?
  trap - EXIT
  docker stop --time 5 "$preflight_name" >/dev/null 2>&1 || true
  docker stop --time 5 "$container_name" >/dev/null 2>&1 || true
  rm -f -- "$launcher_output"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

forwarded=("${retry_args[@]}")
[[ $plan_only != true ]] || forwarded+=(--plan-only)

container_runtime=(
  --pull never --platform linux/amd64 --user 0:0 --read-only
  --tmpfs /tmp:rw,exec,nosuid,nodev,mode=1777,size=536870912
  --tmpfs /private/tmp:rw,exec,nosuid,nodev,mode=1777,size=536870912
  --tmpfs /root:rw,nosuid,nodev,mode=0700,size=268435456
  --tmpfs /run/test-flow-scratch:rw,exec,nosuid,nodev,mode=0700,size=1073741824
  --mount "type=bind,src=$repo_root,dst=$repo_root,readonly"
  --mount "type=bind,src=$cache_root,dst=/cache,readonly"
  --mount "type=bind,src=$evidence_root,dst=/evidence"
  --mount "type=bind,src=$seal,dst=/run/secrets/image-seal.json,readonly"
  --mount "type=bind,src=$codex_auth,dst=/run/secrets/codex-auth.json,readonly"
  --mount "type=bind,src=$claude_settings,dst=/run/secrets/claude-settings.json,readonly"
  --security-opt seccomp=unconfined
  --env HOME=/root
  --env "TEST_FLOW_HOST_UID=$host_uid"
  --env "TEST_FLOW_HOST_GID=$host_gid"
  --env TEST_FLOW_QUICK_UBUNTU2204_CONTAINER=1
  --env TEST_FLOW_QUICK_SCRATCH_ROOT=/run/test-flow-scratch
  --env TEST_FLOW_PYTHON=/opt/venvs/xiaodao/bin/python
  --env TEST_FLOW_QUICK_PYTHON=/opt/venvs/xiaodao/bin/python
  --env GIT_CONFIG_COUNT=1
  --env GIT_CONFIG_KEY_0=safe.directory
  --env "GIT_CONFIG_VALUE_0=$repo_root"
  --workdir "$repo_root"
)

docker run --rm --init --name "$preflight_name" --network none \
  "${container_runtime[@]}" "$image_id" \
  bash -ceu '
    test -x /usr/bin/codex
    /usr/bin/codex --version >/dev/null
    test -x /opt/venvs/xiaodao/bin/python
    /opt/venvs/xiaodao/bin/python --version >/dev/null
    test -r /opt/claude-cache/cache-seal.json
    test -r /opt/claude-cache/package/cli.js
    node /opt/claude-cache/package/cli.js --version >/dev/null
    test -r /run/secrets/image-seal.json
    test -r /run/secrets/codex-auth.json
    test -r /run/secrets/claude-settings.json
    test -r /cache
    test -w /evidence
  '

set +e
docker run --rm --init --name "$container_name" \
  --label "problem-locator.test-flow.container-run=$container_name" \
  --network bridge "${container_runtime[@]}" "$image_id" \
  bash "$tool_root/run.sh" --inside-container "${forwarded[@]}" | tee "$launcher_output"
container_status=${PIPESTATUS[0]}
set -e

if [[ $plan_only != true ]]; then
  set +e
  attempt_root=$(awk '
    /^[[:space:]]*"attempt_root"[[:space:]]*:/ {
      count += 1
      if ($0 !~ /^[[:space:]]*"attempt_root"[[:space:]]*:[[:space:]]*"\/evidence\/run-[^"]+"[[:space:]]*,?[[:space:]]*$/) {
        invalid = 1
        next
      }
      value = $0
      sub(/^[[:space:]]*"attempt_root"[[:space:]]*:[[:space:]]*"/, "", value)
      sub(/"[[:space:]]*,?[[:space:]]*$/, "", value)
    }
    END {
      if (count == 0) exit 4
      if (count != 1 || invalid == 1) exit 5
      print value
    }
  ' "$launcher_output")
  attempt_parse_status=$?
  set -e
  if ((attempt_parse_status != 0 && attempt_parse_status != 4)); then
    printf 'ATTEMPT_ROOT_OUTPUT_INVALID\n' >&2
    exit 3
  fi
  if [[ -n $attempt_root ]]; then
    attempt_name=${attempt_root#/evidence/}
    case "$attempt_root:$attempt_name" in
      /evidence/*:run-*/*|/evidence/*:*/*|/evidence/:*) printf 'ATTEMPT_ROOT_INVALID:%s\n' "$attempt_root" >&2; exit 3 ;;
      /evidence/run-*:run-*) ;;
      *) printf 'ATTEMPT_ROOT_INVALID:%s\n' "$attempt_root" >&2; exit 3 ;;
    esac
    host_attempt="$evidence_root/$attempt_name"
    test -r "$host_attempt/verdict.json" || { printf 'VERDICT_NOT_READABLE:%s\n' "$host_attempt/verdict.json" >&2; exit 3; }
    test "$(stat -c %u:%g "$host_attempt/verdict.json")" = "$host_uid:$host_gid" || { printf 'VERDICT_OWNER_MISMATCH:%s\n' "$host_attempt/verdict.json" >&2; exit 3; }
  elif ((container_status == 0)); then
    printf 'ATTEMPT_ROOT_MISSING\n' >&2
    exit 3
  fi
fi

exit "$container_status"
