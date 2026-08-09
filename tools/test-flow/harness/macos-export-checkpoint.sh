#!/bin/sh
set -eu
umask 077

archive=${1:?checkpoint archive path required}
case "$archive" in
  /tmp/test-flow-stable-state-*.tar) ;;
  *) exit 64 ;;
esac

test "$(id -u)" = 0
data_root=/var/lib/problem-locator
staging="${archive}.root"
test ! -e "$archive"
test ! -e "$staging"

published=0
cleanup() {
  rm -rf "$staging"
  if [ "$published" -ne 1 ]; then rm -f "$archive"; fi
}
trap cleanup EXIT HUP INT TERM

install -d -m 0700 "$staging"
for required in data-format.json state.json resources jobs; do
  test -e "$data_root/$required"
  cp -a "$data_root/$required" "$staging/"
done
if [ -f "$data_root/state.json.prev" ]; then
  cp -a "$data_root/state.json.prev" "$staging/"
fi
for temporary in uploads proposals quarantine state; do
  test -d "$data_root/tmp/$temporary"
  test -z "$(find "$data_root/tmp/$temporary" -mindepth 1 -print -quit)"
done

# Checkpoints contain durable business state only.  The live instance lock and
# retained per-Job workspaces are deliberately excluded.  A restore starts
# with the fixed, empty temporary layout and lets the current service rebuild
# any disposable workspace it needs.
install -d -m 0700 \
  "$staging/tmp" \
  "$staging/tmp/uploads" \
  "$staging/tmp/proposals" \
  "$staging/tmp/workspaces" \
  "$staging/tmp/quarantine" \
  "$staging/tmp/state"

test -z "$(find "$staging" -xdev -type l -print -quit)"
test -z "$(find "$staging" -xdev ! -type d ! -type f -print -quit)"
test -z "$(find "$staging" -xdev -type f -links +1 -print -quit)"

if [ -f "$staging/state.json.prev" ]; then
  set -- data-format.json jobs resources state.json state.json.prev tmp
else
  set -- data-format.json jobs resources state.json tmp
fi
tar --format=ustar --sort=name --mtime=@0 --numeric-owner \
  -C "$staging" -cf "$archive" "$@"
chmod 0600 "$archive"
published=1
