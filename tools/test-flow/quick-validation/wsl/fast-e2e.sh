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
  case "$provider" in
    codex-luna)
      command=(
        bash "$repo_root/tools/test-flow/quick-validation/codex-luna/run.sh"
        --goal fast-e2e
        --codex-entry /usr/bin/codex
        --codex-auth /run/secrets/codex-auth.json
        --python-entry /opt/venvs/xiaodao/bin/python
        --cache-root /cache
        --registration-root /registration
        --runs-root /evidence
      )
      ;;
    claude-deepseek)
      command=(
        bash "$repo_root/tools/test-flow/quick-validation/claude-deepseek/run.sh"
        --goal fast-e2e
        --client macos
        --claude-entry /opt/claude-cache/package/cli.js
        --claude-settings /run/secrets/claude-settings.json
        --python-entry /opt/venvs/xiaodao/bin/python
        --cache-root /cache
        --registration-root /registration
        --runs-root /evidence
      )
      ;;
    *) printf 'PROVIDER_REQUIRED\n' >&2; exit 2 ;;
  esac
  exec "${command[@]}" "$@"
fi

mode=""
provider=""
cache_root=""
registration_root=""
evidence_root=""
codex_auth=""
claude_settings=""
seal=""
plan_only=false
allow_real_model=false
retry_args=()

while (($#)); do
  case "$1" in
    --mode)
      (($# >= 2)) || { printf 'ARGUMENT_VALUE_REQUIRED:%s\n' "$1" >&2; exit 2; }
      mode=$2
      shift 2
      ;;
    --provider)
      (($# >= 2)) || { printf 'ARGUMENT_VALUE_REQUIRED:%s\n' "$1" >&2; exit 2; }
      provider=$2
      shift 2
      ;;
    --cache-root) cache_root=$2; shift 2 ;;
    --registration-root) registration_root=$2; shift 2 ;;
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
    --allow-real-model) allow_real_model=true; shift ;;
    --scenario|--all-scenarios)
      printf 'FAST_E2E_ALWAYS_RUNS_NINE_SCENARIOS\n' >&2
      exit 2
      ;;
    --goal|--track|--client|--repo-root|--codex-entry|--claude-entry|--runs-root|--source-snapshot-digest|--core-verdict)
      printf 'PROVIDER_OR_RELEASE_OWNED_ARGUMENT:%s\n' "$1" >&2
      exit 2
      ;;
    *) printf 'UNKNOWN_WRAPPER_ARGUMENT:%s\n' "$1" >&2; exit 2 ;;
  esac
done

[[ $mode == "fast-e2e" ]] || { printf 'FAST_E2E_MODE_REQUIRED\n' >&2; exit 2; }
case "$provider" in codex-luna|claude-deepseek) ;; *) printf 'PROVIDER_REQUIRED\n' >&2; exit 2 ;; esac
test -n "$cache_root" || { printf 'CACHE_ROOT_REQUIRED\n' >&2; exit 2; }
test -n "$registration_root" || { printf 'REGISTRATION_ROOT_REQUIRED\n' >&2; exit 2; }
test -n "$evidence_root" || { printf 'EVIDENCE_ROOT_REQUIRED\n' >&2; exit 2; }
case "$cache_root:$registration_root:$evidence_root" in /*:/*:/*) ;; *) printf 'ABSOLUTE_INPUT_OUTPUT_PATHS_REQUIRED\n' >&2; exit 2 ;; esac
case "$provider" in
  codex-luna)
    test -n "$codex_auth" || { printf 'CODEX_AUTH_REQUIRED\n' >&2; exit 2; }
    [[ $codex_auth == /* ]] || { printf 'ABSOLUTE_CODEX_AUTH_REQUIRED\n' >&2; exit 2; }
    ;;
  claude-deepseek)
    test -n "$claude_settings" || { printf 'CLAUDE_SETTINGS_REQUIRED\n' >&2; exit 2; }
    [[ $claude_settings == /* ]] || { printf 'ABSOLUTE_CLAUDE_SETTINGS_REQUIRED\n' >&2; exit 2; }
    ;;
esac
case "$(uname -r)" in *[Mm]icrosoft*WSL2*|*[Mm]icrosoft-standard-WSL2*) ;; *) printf 'WSL2_REQUIRED\n' >&2; exit 2 ;; esac
test "$(. /etc/os-release && printf '%s' "$ID:$VERSION_ID")" = "ubuntu:22.04" || { printf 'UBUNTU_2204_HOST_REQUIRED\n' >&2; exit 2; }
test "$(stat -f -c %T "$repo_root")" = "ext2/ext3" || { printf 'EXT4_REPO_REQUIRED\n' >&2; exit 2; }
command -v docker >/dev/null || { printf 'DOCKER_REQUIRED\n' >&2; exit 2; }
test "$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')" = "linux/amd64" || { printf 'DOCKER_LINUX_AMD64_REQUIRED\n' >&2; exit 2; }
test -d "$cache_root" || { printf 'CACHE_ROOT_MISSING\n' >&2; exit 2; }
test -d "$registration_root" || { printf 'REGISTRATION_ROOT_MISSING\n' >&2; exit 2; }
case "$provider" in
  codex-luna) test -f "$codex_auth" || { printf 'CODEX_AUTH_MISSING\n' >&2; exit 2; } ;;
  claude-deepseek) test -f "$claude_settings" || { printf 'CLAUDE_SETTINGS_MISSING\n' >&2; exit 2; } ;;
esac

seal=${seal:-"$cache_root/quick-validation/ubuntu2204-central/image-seal.json"}
test -f "$seal" || { printf 'IMAGE_SEAL_MISSING\n' >&2; exit 2; }
image_id=$(sed -n 's/.*"image_id":"\([^"]*\)".*/\1/p' "$seal")
test -n "$image_id" || { printf 'IMAGE_SEAL_INVALID\n' >&2; exit 2; }
test "$(docker image inspect "$image_id" --format '{{.Id}}')" = "$image_id" || { printf 'IMAGE_ID_DRIFT\n' >&2; exit 2; }
test "$(docker image inspect "$image_id" --format '{{index .Config.Labels "problem-locator.quick.container"}}')" = "ubuntu22.04-central-v1" || { printf 'IMAGE_PROFILE_INVALID\n' >&2; exit 2; }

mkdir -p -m 700 "$evidence_root"
host_uid=$(id -u)
host_gid=$(id -g)
container_prefix="pltf-fast-e2e-${provider}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
owned_containers=()
plan_work=""

cleanup() {
  local status=$?
  trap - EXIT
  for owned in "${owned_containers[@]}"; do
    docker stop --time 5 "$owned" >/dev/null 2>&1 || true
  done
  if [[ -n $plan_work && $plan_work == /tmp/pltf-fast-e2e-plan.* && -d $plan_work ]]; then
    rm -rf -- "$plan_work"
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

provider_mounts=(
  --mount "type=bind,src=$repo_root,dst=$repo_root,readonly"
  --mount "type=bind,src=$cache_root,dst=/cache,readonly"
  --mount "type=bind,src=$registration_root,dst=/registration,readonly"
  --mount "type=bind,src=$seal,dst=/run/secrets/image-seal.json,readonly"
)
provider_security=()
case "$provider" in
  codex-luna)
    provider_mounts+=(--mount "type=bind,src=$codex_auth,dst=/run/secrets/codex-auth.json,readonly")
    provider_security+=(--security-opt seccomp=unconfined)
    ;;
  claude-deepseek)
    provider_mounts+=(--mount "type=bind,src=$claude_settings,dst=/run/secrets/claude-settings.json,readonly")
    ;;
esac

run_provider_container() {
  local container_name=$1
  local container_evidence=$2
  shift 2
  docker run --rm --init --name "$container_name" \
    --label "problem-locator.fast-e2e.container-run=$container_name" \
    --pull never --platform linux/amd64 --user 0:0 --read-only --network bridge \
    --tmpfs /tmp:rw,exec,nosuid,nodev,mode=1777,size=536870912 \
    --tmpfs /private/tmp:rw,exec,nosuid,nodev,mode=1777,size=536870912 \
    --tmpfs /root:rw,nosuid,nodev,mode=0700,size=268435456 \
    --tmpfs /run/test-flow-scratch:rw,exec,nosuid,nodev,mode=0700,size=1073741824 \
    "${provider_mounts[@]}" \
    --mount "type=bind,src=$container_evidence,dst=/evidence" \
    "${provider_security[@]}" \
    --env HOME=/root \
    --env TEST_FLOW_QUICK_UBUNTU2204_CONTAINER=1 \
    --env TEST_FLOW_QUICK_SCRATCH_ROOT=/run/test-flow-scratch \
    --env TEST_FLOW_PYTHON=/opt/venvs/xiaodao/bin/python \
    --env TEST_FLOW_QUICK_PYTHON=/opt/venvs/xiaodao/bin/python \
    --env GIT_CONFIG_COUNT=1 \
    --env GIT_CONFIG_KEY_0=safe.directory \
    --env "GIT_CONFIG_VALUE_0=$repo_root" \
    --workdir "$repo_root" "$image_id" \
    bash "$tool_root/fast-e2e.sh" --inside-container "$provider" -- "$@"
}

run_utility_container() {
  local container_name=$1
  local suite_mount=$2
  shift 2
  local utility_mounts=(
    --mount "type=bind,src=$repo_root,dst=$repo_root,readonly"
    --mount "type=bind,src=$seal,dst=/run/secrets/image-seal.json,readonly"
  )
  if [[ -n $plan_work ]]; then
    utility_mounts+=(--mount "type=bind,src=$plan_work,dst=/plan-work,readonly")
  fi
  if [[ -n $suite_mount ]]; then
    utility_mounts+=(--mount "type=bind,src=$suite_mount,dst=/suite")
  fi
  docker run --rm --init --name "$container_name" \
    --label "problem-locator.fast-e2e.container-run=$container_name" \
    --pull never --platform linux/amd64 --user 0:0 --read-only --network none \
    --tmpfs /tmp:rw,exec,nosuid,nodev,mode=1777,size=67108864 \
    --tmpfs /private/tmp:rw,exec,nosuid,nodev,mode=1777,size=67108864 \
    --tmpfs /root:rw,nosuid,nodev,mode=0700,size=67108864 \
    "${utility_mounts[@]}" \
    --workdir "$repo_root" "$image_id" \
    node "$tool_root/container-suite.mjs" "$@"
}

transfer_suite_ownership() {
  local container_name=$1
  local suite_mount=$2
  docker run --rm --init --name "$container_name" \
    --pull never --platform linux/amd64 --user 0:0 --read-only --network none \
    --mount "type=bind,src=$suite_mount,dst=/suite" \
    --env "TEST_FLOW_HOST_UID=$host_uid" \
    --env "TEST_FLOW_HOST_GID=$host_gid" \
    "$image_id" \
    bash -ceu '[[ $TEST_FLOW_HOST_UID =~ ^[0-9]+$ ]]; [[ $TEST_FLOW_HOST_GID =~ ^[0-9]+$ ]]; chown -R -- "$TEST_FLOW_HOST_UID:$TEST_FLOW_HOST_GID" /suite'
}

preflight_name="${container_prefix}-preflight"
owned_containers+=("$preflight_name")
preflight_checks=(
  'test -x /opt/venvs/xiaodao/bin/python'
  '/opt/venvs/xiaodao/bin/python --version >/dev/null'
  'test -r /run/secrets/image-seal.json'
  'test -r /cache'
  'test -r /registration/registration-template.json'
  'test -w /evidence'
)
case "$provider" in
  codex-luna)
    preflight_checks+=(
      'test -x /usr/bin/codex'
      '/usr/bin/codex --version >/dev/null'
      'test -r /run/secrets/codex-auth.json'
    )
    ;;
  claude-deepseek)
    preflight_checks+=(
      'test -r /opt/claude-cache/cache-seal.json'
      'test -r /opt/claude-cache/package/cli.js'
      'node /opt/claude-cache/package/cli.js --version >/dev/null'
      'test -r /run/secrets/claude-settings.json'
    )
    ;;
esac
preflight_script=$(printf '%s\n' "${preflight_checks[@]}")
docker run --rm --init --name "$preflight_name" --network none \
  --pull never --platform linux/amd64 --user 0:0 --read-only \
  --tmpfs /tmp:rw,exec,nosuid,nodev,mode=1777,size=67108864 \
  --tmpfs /root:rw,nosuid,nodev,mode=0700,size=67108864 \
  "${provider_mounts[@]}" \
  --mount "type=bind,src=$evidence_root,dst=/evidence" \
  "${provider_security[@]}" \
  "$image_id" bash -ceu "$preflight_script"

plan_work=$(mktemp -d /tmp/pltf-fast-e2e-plan.XXXXXX)
mkdir -p -m 700 "$plan_work/evidence"
provider_plan_container="${container_prefix}-provider-plan"
owned_containers+=("$provider_plan_container")
if ! run_provider_container "$provider_plan_container" "$plan_work/evidence" \
  --all-scenarios --plan-only "${retry_args[@]}" \
  > "$plan_work/provider-plan.json" \
  2> "$plan_work/provider-plan.stderr.txt"; then
  sed -n '1,240p' "$plan_work/provider-plan.stderr.txt" >&2
  printf 'PROVIDER_SUITE_PLAN_FAILED\n' >&2
  exit 2
fi

if [[ $plan_only == true ]]; then
  plan_container="${container_prefix}-container-plan"
  owned_containers+=("$plan_container")
  run_utility_container "$plan_container" "" plan \
    --provider "$provider" \
    --provider-plan /plan-work/provider-plan.json \
    --image-seal /run/secrets/image-seal.json
  exit $?
fi

[[ $allow_real_model == true ]] || { printf 'REAL_MODEL_OPT_IN_REQUIRED\n' >&2; exit 2; }

suite_id="wsl-${provider}-fast-e2e-$(date -u +%Y%m%dT%H%M%SZ)-$$"
suite_root="$evidence_root/$suite_id"
mkdir -m 700 "$suite_root"
mkdir -p -m 700 "$suite_root/.children" "$suite_root/scenarios" "$suite_root/evidence/container-runtime"
printf '%s\n' "$suite_id" > "$suite_root/run-id.txt"
date -u +%Y-%m-%dT%H:%M:%S.000Z > "$suite_root/started-at.txt"
cp "$plan_work/provider-plan.json" "$suite_root/evidence/container-runtime/provider-plan.json"
cp "$plan_work/provider-plan.stderr.txt" "$suite_root/evidence/container-runtime/provider-plan.stderr.txt"

plan_container="${container_prefix}-container-plan"
owned_containers+=("$plan_container")
run_utility_container "$plan_container" "$suite_root" plan \
  --provider "$provider" \
  --provider-plan /plan-work/provider-plan.json \
  --image-seal /run/secrets/image-seal.json \
  --output /suite/plan.json \
  > "$suite_root/evidence/container-runtime/container-plan.stdout.txt"

admission_container="${container_prefix}-admission"
owned_containers+=("$admission_container")
set +e
run_utility_container "$admission_container" "$suite_root" admission \
  --plan /suite/plan.json \
  > "$suite_root/evidence/container-runtime/admission.json"
admission_code=$?
set -e
if ((admission_code != 0)); then
  aggregate_container="${container_prefix}-aggregate-blocked"
  ownership_container="${container_prefix}-ownership-blocked"
  owned_containers+=("$aggregate_container" "$ownership_container")
  set +e
  run_utility_container "$aggregate_container" "$suite_root" aggregate \
    --provider "$provider" --suite-root /suite --display-root "$suite_root"
  aggregate_code=$?
  set -e
  transfer_suite_ownership "$ownership_container" "$suite_root"
  exit "$aggregate_code"
fi

scenarios=(
  api-execution-overrun
  client-receive-blocked
  deadloop-detected
  insufficient-evidence
  multiple-rpc-timeouts
  server-queue-delay
  server-queue-five
  server-queue-single
  unrelated-log-noise
)

run_scenario_container() {
  local scenario_id=$1
  local container_name=$2
  local raw_root="$suite_root/.children/$scenario_id"
  local runtime_root="$suite_root/evidence/container-runtime/$scenario_id"
  mkdir -p -m 700 "$raw_root" "$runtime_root"
  printf '%s\n' "$container_name" > "$runtime_root/container-name.txt"
  set +e
  run_provider_container "$container_name" "$raw_root" \
    --scenario "$scenario_id" --allow-real-model "${retry_args[@]}" \
    > "$runtime_root/stdout.txt" \
    2> "$runtime_root/stderr.txt"
  local exit_code=$?
  set -e
  printf '%s\n' "$exit_code" > "$runtime_root/exit-code.txt"
}

pids=()
for scenario_id in "${scenarios[@]}"; do
  scenario_container="${container_prefix}-${scenario_id}"
  owned_containers+=("$scenario_container")
  run_scenario_container "$scenario_id" "$scenario_container" &
  pids+=("$!")
done
for child_pid in "${pids[@]}"; do
  wait "$child_pid" || true
done

materialize_container="${container_prefix}-materialize"
owned_containers+=("$materialize_container")
set +e
run_utility_container "$materialize_container" "$suite_root" materialize \
  --suite-root /suite \
  > "$suite_root/evidence/container-runtime/materialize.stdout.txt" \
  2> "$suite_root/evidence/container-runtime/materialize.stderr.txt"
materialize_code=$?
set -e
printf '%s\n' "$materialize_code" > "$suite_root/evidence/container-runtime/materialize-exit-code.txt"

aggregate_container="${container_prefix}-aggregate"
ownership_container="${container_prefix}-ownership"
owned_containers+=("$aggregate_container" "$ownership_container")
set +e
run_utility_container "$aggregate_container" "$suite_root" aggregate \
  --provider "$provider" --suite-root /suite --display-root "$suite_root"
aggregate_code=$?
set -e
transfer_suite_ownership "$ownership_container" "$suite_root"
exit "$aggregate_code"
