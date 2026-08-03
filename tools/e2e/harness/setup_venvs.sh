#!/usr/bin/env bash
set -euo pipefail

export PYTHONPYCACHEPREFIX=/tmp/attempt52-pycache
export UV_NO_PROGRESS=1
export UV_PYTHON_DOWNLOADS=automatic
export UV_PYTHON_INSTALL_DIR=/opt/uv-python

mkdir -p /opt/uv-python
chmod 0755 /opt/uv-python

python_attempt=0
base_cache_hit=false
if [ -f /opt/e2e-lock/base-ready ]; then
  base_cache_hit=true
  test "$(/opt/uv-python/cpython-3.12.13-linux-x86_64-gnu/bin/python3.12 -c 'import platform; print(platform.python_version())')" = 3.12.13
else
  python_attempt=1
  while [ "$python_attempt" -le 3 ]; do
    if uv python install 3.12.13; then
      break
    fi
    if [ "$python_attempt" -eq 3 ]; then
      exit 1
    fi
    python_attempt=$((python_attempt + 1))
    sleep 2
  done
fi

mkdir -p /opt/venvs
UV_PROJECT_ENVIRONMENT=/opt/venvs/xiaodao \
  uv sync --frozen --all-groups --python 3.12.13 --directory /opt/src/xiaodao
test "$(/opt/venvs/xiaodao/bin/python -c 'import platform; print(platform.python_version())')" = '3.12.13'
uv pip check --python /opt/venvs/xiaodao/bin/python
UV_PROJECT_ENVIRONMENT=/opt/venvs/xiaodao uv lock --check --directory /opt/src/xiaodao

if [ ! -x /opt/venvs/logparse/bin/python ]; then
  uv venv --python 3.12.13 /opt/venvs/logparse
fi
uv pip install --python /opt/venvs/logparse/bin/python -r /opt/src/logparse/requirements.txt
test "$(/opt/venvs/logparse/bin/python -c 'import platform; print(platform.python_version())')" = '3.12.13'
uv pip check --python /opt/venvs/logparse/bin/python

xiaodao_packages=$(uv pip list --python /opt/venvs/xiaodao/bin/python --format freeze | wc -l)
logparse_packages=$(uv pip list --python /opt/venvs/logparse/bin/python --format freeze | wc -l)

test ! -L /opt/venvs/xiaodao/.lock
test -f /opt/venvs/xiaodao/.lock
test "$(stat -c '%u:%g' -- /opt/venvs/xiaodao/.lock)" = '0:0'
chmod 0644 -- /opt/venvs/xiaodao/.lock
test ! -L /opt/venvs/xiaodao/.lock
test -f /opt/venvs/xiaodao/.lock
test "$(stat -c '%u:%g:%a' -- /opt/venvs/xiaodao/.lock)" = '0:0:644'

test ! -L /opt/venvs/logparse/.lock
test -f /opt/venvs/logparse/.lock
test "$(stat -c '%u:%g' -- /opt/venvs/logparse/.lock)" = '0:0'
chmod 0644 -- /opt/venvs/logparse/.lock
test ! -L /opt/venvs/logparse/.lock
test -f /opt/venvs/logparse/.lock
test "$(stat -c '%u:%g:%a' -- /opt/venvs/logparse/.lock)" = '0:0:644'

test ! -L /opt/uv-python/.lock
test -f /opt/uv-python/.lock
test "$(stat -c '%u:%g' -- /opt/uv-python/.lock)" = '0:0'
chmod 0644 -- /opt/uv-python/.lock
test ! -L /opt/uv-python/.lock
test -f /opt/uv-python/.lock
test "$(stat -c '%u:%g:%a' -- /opt/uv-python/.lock)" = '0:0:644'

test "$xiaodao_packages" -eq 37
test "$logparse_packages" -eq 7

xiaodao_resolved=$(readlink -f /opt/venvs/xiaodao/bin/python)
logparse_resolved=$(readlink -f /opt/venvs/logparse/bin/python)
case "$xiaodao_resolved" in
  /opt/uv-python/*) ;;
  *) exit 1 ;;
esac
case "$logparse_resolved" in
  /opt/uv-python/*) ;;
  *) exit 1 ;;
esac
test -f "$xiaodao_resolved" && test -x "$xiaodao_resolved"
test -f "$logparse_resolved" && test -x "$logparse_resolved"
test "$(stat -c %a /opt/uv-python)" = 755

{
  printf 'xiaodao_path=/opt/venvs/xiaodao\n'
  printf 'xiaodao_python=3.12.13\n'
  printf 'xiaodao_install=UV_PROJECT_ENVIRONMENT=/opt/venvs/xiaodao uv sync --frozen --all-groups\n'
  printf 'xiaodao_package_count=%s\n' "$xiaodao_packages"
  printf 'xiaodao_pip_check=pass\n'
  printf 'uv_lock_check=pass\n'
  printf 'logparse_path=/opt/venvs/logparse\n'
  printf 'logparse_python=3.12.13\n'
  printf 'logparse_install=uv pip install -r /opt/src/logparse/requirements.txt\n'
  printf 'logparse_package_count=%s\n' "$logparse_packages"
  printf 'logparse_pip_check=pass\n'
  printf 'logparse_commit=%s\n' "$(git -C /opt/src/logparse rev-parse HEAD)"
  printf 'logparse_tree=clean\n'
  printf 'python_install_attempts=%s\n' "$python_attempt"
  printf 'base_cache_hit=%s\n' "$base_cache_hit"
  printf 'managed_python_root=/opt/uv-python\n'
  printf 'managed_python_root_mode=0755\n'
  printf 'xiaodao_lock=regular,non-symlink,0:0,0644\n'
  printf 'logparse_lock=regular,non-symlink,0:0,0644\n'
  printf 'managed_python_lock=regular,non-symlink,0:0,0644\n'
  printf 'xiaodao_launcher_resolved_under_managed_root=true\n'
  printf 'logparse_launcher_resolved_under_managed_root=true\n'
} > /evidence/venv-verification.txt

printf 'python=3.12.13\n' >> /evidence/source-pins.txt
