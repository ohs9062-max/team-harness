#!/usr/bin/env bash
# Exercise the full suite with a real watchd-named ancestor.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
tmp=$(mktemp -d "${TMPDIR:-/tmp}/th-test-watchd.XXXXXXXX")
trap 'rm -rf -- "$tmp"' EXIT
cat > "$tmp/th-watchd.sh" <<'WRAPPER'
#!/usr/bin/env bash
bash "$1/scripts/th-test.sh" src/tests/ -q -p no:cacheprovider
rc=$?
exit "$rc"
WRAPPER
chmod +x "$tmp/th-watchd.sh"
"$tmp/th-watchd.sh" "$root"
