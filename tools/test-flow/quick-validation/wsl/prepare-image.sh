#!/usr/bin/env bash
set -euo pipefail

tool_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$tool_root/../../../.." && pwd)
cache_root=""
codex_root=""
logparse_source=""
image_tag="problem-locator-quick-validation:ubuntu22.04-central-v1"

while (($#)); do
  case "$1" in
    --cache-root) cache_root=$2; shift 2 ;;
    --codex-root) codex_root=$2; shift 2 ;;
    --logparse-source) logparse_source=$2; shift 2 ;;
    --image-tag) image_tag=$2; shift 2 ;;
    *) printf 'UNKNOWN_ARGUMENT:%s\n' "$1" >&2; exit 2 ;;
  esac
done

test -n "$cache_root" || { printf 'CACHE_ROOT_REQUIRED\n' >&2; exit 2; }
test -n "$codex_root" || { printf 'CODEX_ROOT_REQUIRED\n' >&2; exit 2; }
test -n "$logparse_source" || { printf 'LOGPARSE_SOURCE_REQUIRED\n' >&2; exit 2; }
case "$cache_root:$codex_root:$logparse_source" in /*:/*:/*) ;; *) printf 'ABSOLUTE_PATHS_REQUIRED\n' >&2; exit 2;; esac
case "$(uname -r)" in *[Mm]icrosoft*WSL2*|*[Mm]icrosoft-standard-WSL2*) ;; *) printf 'WSL2_REQUIRED\n' >&2; exit 2;; esac
test "$(. /etc/os-release && printf '%s' "$ID:$VERSION_ID")" = "ubuntu:22.04" || { printf 'UBUNTU_2204_HOST_REQUIRED\n' >&2; exit 2; }
test "$(stat -f -c %T "$repo_root")" = "ext2/ext3" || { printf 'EXT4_REPO_REQUIRED\n' >&2; exit 2; }
command -v docker >/dev/null || { printf 'DOCKER_REQUIRED\n' >&2; exit 2; }
test "$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')" = "linux/amd64" || { printf 'DOCKER_LINUX_AMD64_REQUIRED\n' >&2; exit 2; }
test -f "$codex_root/codex" && test -f "$codex_root/codex-code-mode-host" || { printf 'CODEX_CACHE_INCOMPLETE\n' >&2; exit 2; }
test "$(git -C "$logparse_source" rev-parse HEAD)" = "a233b500d9c99e6815d1ffd82cb4ca55bbfe657a" || { printf 'LOGPARSE_COMMIT_MISMATCH\n' >&2; exit 2; }
test -z "$(git -C "$logparse_source" status --porcelain)" || { printf 'LOGPARSE_DIRTY\n' >&2; exit 2; }

node_cache="$cache_root/node/v24.16.0"
claude_cache="$cache_root/claude/2.1.89"
uv_cache="$cache_root/uv/0.11.32"
test -f "$node_cache/node-v24.16.0-linux-x64.tar.xz" || { printf 'NODE_CACHE_INCOMPLETE\n' >&2; exit 2; }
test -f "$claude_cache/package/cli.js" && test -f "$claude_cache/cache-seal.json" || { printf 'CLAUDE_CACHE_INCOMPLETE\n' >&2; exit 2; }
test -f "$uv_cache/uv" && test -f "$uv_cache/uvx" || { printf 'UV_CACHE_INCOMPLETE\n' >&2; exit 2; }

docker buildx build --load --platform linux/amd64 \
  --tag "$image_tag" \
  --build-context "nodecache=$node_cache" \
  --build-context "codexcache=$codex_root" \
  --build-context "claudecache=$claude_cache" \
  --build-context "uvcache=$uv_cache" \
  --build-context "logparse=$logparse_source" \
  --file "$tool_root/Dockerfile" "$repo_root"

image_id=$(docker image inspect "$image_tag" --format '{{.Id}}')
test "$(docker image inspect "$image_id" --format '{{index .Config.Labels "problem-locator.quick.container"}}')" = "ubuntu22.04-central-v1"
docker run --rm --init --pull never --platform linux/amd64 --user 0:0 --read-only --network none \
  --tmpfs /tmp:rw,exec,nosuid,nodev,mode=1777,size=67108864 \
  --tmpfs /private/tmp:rw,exec,nosuid,nodev,mode=1777,size=67108864 \
  "$image_id" /bin/bash -lc '
set -euo pipefail
test "$(cat /proc/1/comm)" = "docker-init"
test "$(. /etc/os-release && printf "%s:%s" "$ID" "$VERSION_ID")" = "ubuntu:22.04"
test "$(node --version)" = "v24.16.0"
test "$(/usr/bin/codex --version)" = "codex-cli 0.149.1"
test "$(/opt/node/bin/node /opt/claude-cache/package/cli.js --version)" = "2.1.89 (Claude Code)"
test "$(/opt/venvs/xiaodao/bin/python --version)" = "Python 3.12.13"
test "$(/usr/bin/python3 --version)" = "Python 3.12.13"
test "$(/opt/logparse/.venv/bin/python --version)" = "Python 3.12.13"
/opt/logparse/.venv/bin/python -I -c "import sys; assert sys.prefix == \"/opt/logparse/.venv\""
test -w /private/tmp
/opt/venvs/xiaodao/bin/python -I -c "import problem_locator"
test "$(/usr/bin/stat -f %z /opt/logparse-commit)" = "41"
test -z "$(git -C /opt/logparse status --porcelain)"
'

seal_root="$cache_root/quick-validation/ubuntu2204-central"
mkdir -p -m 700 "$seal_root"
temporary="$seal_root/image-seal.json.tmp.$$"
printf '%s' "{\"image_id\":\"$image_id\",\"image_tag\":\"$image_tag\",\"platform\":\"linux/amd64\",\"profile\":\"ubuntu22.04-central-v1\",\"schema_version\":1,\"status\":\"PASS\"}" > "$temporary"
chmod 0600 "$temporary"
mv "$temporary" "$seal_root/image-seal.json"
printf '%s\n' "$seal_root/image-seal.json"
