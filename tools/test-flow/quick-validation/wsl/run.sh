#!/usr/bin/env bash
set -euo pipefail

tool_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$tool_root/../../../.." && pwd)

if [[ ${1:-} == "--inside-container" ]]; then
  shift
  provider=${1:-}
  shift || true
  [[ ${1:-} == "--" ]] || { printf 'CONTAINER_FORWARD_SEPARATOR_REQUIRED\n' >&2; exit 2; }
  shift
  common=(
    --track dev
    --client macos
    --repo-root "$repo_root"
    --cache-root /cache
    --evidence-root /evidence
  )
  case "$provider" in
    codex-luna)
      command=(bash "$repo_root/tools/test-flow/run.sh" "${common[@]}"
        --codex-entry /usr/bin/codex
        --codex-auth /run/secrets/codex-auth.json
        --logparse-source /opt/logparse
        "$@")
      ;;
    claude-deepseek)
      command=(bash "$repo_root/tools/test-flow/run.sh" "${common[@]}"
        --claude-entry /opt/claude-cache/package/cli.js
        --claude-settings /run/secrets/claude-settings.json
        --logparse-source /opt/logparse
        "$@")
      ;;
    *) printf 'PROVIDER_INVALID\n' >&2; exit 2 ;;
  esac
  exec "${command[@]}"
fi

provider=""
cache_root=""
evidence_root=""
codex_auth=""
claude_settings=""
seal=""
forwarded=()

while (($#)); do
  case "$1" in
    --provider) provider=$2; shift 2 ;;
    --cache-root) cache_root=$2; shift 2 ;;
    --evidence-root) evidence_root=$2; shift 2 ;;
    --codex-auth) codex_auth=$2; shift 2 ;;
    --claude-settings) claude_settings=$2; shift 2 ;;
    --image-seal) seal=$2; shift 2 ;;
    --) shift; forwarded=("$@"); break ;;
    *) printf 'UNKNOWN_WRAPPER_ARGUMENT:%s\n' "$1" >&2; exit 2 ;;
  esac
done

case "$provider" in codex-luna|claude-deepseek) ;; *) printf 'PROVIDER_REQUIRED\n' >&2; exit 2;; esac
test -n "$cache_root" || { printf 'CACHE_ROOT_REQUIRED\n' >&2; exit 2; }
test -n "$evidence_root" || { printf 'EVIDENCE_ROOT_REQUIRED\n' >&2; exit 2; }
case "$cache_root:$evidence_root" in /*:/*) ;; *) printf 'ABSOLUTE_OUTPUT_PATHS_REQUIRED\n' >&2; exit 2;; esac
goal=""
for ((index=0; index<${#forwarded[@]}; index+=1)); do
  argument=${forwarded[$index]}
  case "$argument" in
    --goal)
      test -z "$goal" || { printf 'GOAL_DUPLICATE\n' >&2; exit 2; }
      test $((index + 1)) -lt ${#forwarded[@]} || { printf 'GOAL_VALUE_REQUIRED\n' >&2; exit 2; }
      goal=${forwarded[$((index + 1))]}
      index=$((index + 1))
      ;;
    --track|--client|--repo-root|--codex-entry|--codex-auth|--claude-entry|--claude-settings|--cache-root|--evidence-root|--logparse-source)
      printf 'CONTAINER_OWNED_ARGUMENT:%s\n' "$argument" >&2
      exit 2
      ;;
  esac
done
case "$provider:$goal" in
  codex-luna:dev.macos-codex-luna-methods|codex-luna:dev.macos-codex-luna-e2e) ;;
  claude-deepseek:dev.macos-claude-deepseek-methods|claude-deepseek:dev.macos-claude-deepseek-e2e) ;;
  *:) printf 'GOAL_REQUIRED\n' >&2; exit 2 ;;
  *) printf 'PROVIDER_GOAL_MISMATCH\n' >&2; exit 2 ;;
esac
case "$(uname -r)" in *[Mm]icrosoft*WSL2*|*[Mm]icrosoft-standard-WSL2*) ;; *) printf 'WSL2_REQUIRED\n' >&2; exit 2;; esac
test "$(. /etc/os-release && printf '%s' "$ID:$VERSION_ID")" = "ubuntu:22.04" || { printf 'UBUNTU_2204_HOST_REQUIRED\n' >&2; exit 2; }
test "$(stat -f -c %T "$repo_root")" = "ext2/ext3" || { printf 'EXT4_REPO_REQUIRED\n' >&2; exit 2; }
command -v docker >/dev/null || { printf 'DOCKER_REQUIRED\n' >&2; exit 2; }
test "$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')" = "linux/amd64" || { printf 'DOCKER_LINUX_AMD64_REQUIRED\n' >&2; exit 2; }
mkdir -p -m 700 "$cache_root" "$evidence_root"
seal=${seal:-"$cache_root/quick-validation/ubuntu2204-central/image-seal.json"}
test -f "$seal" || { printf 'IMAGE_SEAL_MISSING\n' >&2; exit 2; }
image_id=$(sed -n 's/.*"image_id":"\([^"]*\)".*/\1/p' "$seal")
test -n "$image_id" || { printf 'IMAGE_SEAL_INVALID\n' >&2; exit 2; }
test "$(docker image inspect "$image_id" --format '{{.Id}}')" = "$image_id" || { printf 'IMAGE_ID_DRIFT\n' >&2; exit 2; }
test "$(docker image inspect "$image_id" --format '{{index .Config.Labels "problem-locator.quick.container"}}')" = "ubuntu22.04-central-v1" || { printf 'IMAGE_PROFILE_INVALID\n' >&2; exit 2; }

mounts=(
  --mount "type=bind,src=$repo_root,dst=$repo_root,readonly"
  --mount "type=bind,src=$cache_root,dst=/cache"
  --mount "type=bind,src=$evidence_root,dst=/evidence"
  --mount "type=bind,src=$seal,dst=/run/secrets/image-seal.json,readonly"
)
security=()
case "$provider" in
  codex-luna)
    test -f "$codex_auth" || { printf 'CODEX_AUTH_MISSING\n' >&2; exit 2; }
    mounts+=(--mount "type=bind,src=$codex_auth,dst=/run/secrets/codex-auth.json,readonly")
    security+=(--security-opt seccomp=unconfined)
    ;;
  claude-deepseek)
    test -f "$claude_settings" || { printf 'CLAUDE_SETTINGS_MISSING\n' >&2; exit 2; }
    mounts+=(--mount "type=bind,src=$claude_settings,dst=/run/secrets/claude-settings.json,readonly")
    ;;
esac

container="pltf-quick-ubuntu2204-${provider}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
exec docker run --rm --init --name "$container" \
  --label "problem-locator.quick.container-run=$container" \
  --pull never --platform linux/amd64 --user 0:0 --read-only --network bridge \
  --tmpfs /tmp:rw,exec,nosuid,nodev,mode=1777,size=536870912 \
  --tmpfs /private/tmp:rw,exec,nosuid,nodev,mode=1777,size=536870912 \
  --tmpfs /root:rw,nosuid,nodev,mode=0700,size=268435456 \
  --tmpfs /run/test-flow-scratch:rw,exec,nosuid,nodev,mode=0700,size=1073741824 \
  "${mounts[@]}" "${security[@]}" \
  --env TEST_FLOW_QUICK_UBUNTU2204_CONTAINER=1 \
  --env TEST_FLOW_QUICK_SCRATCH_ROOT=/run/test-flow-scratch \
  --env TEST_FLOW_PYTHON=/opt/venvs/xiaodao/bin/python \
  --env GIT_CONFIG_COUNT=1 \
  --env GIT_CONFIG_KEY_0=safe.directory \
  --env "GIT_CONFIG_VALUE_0=$repo_root" \
  --workdir "$repo_root" "$image_id" \
  bash "$tool_root/run.sh" --inside-container "$provider" -- "${forwarded[@]}"
