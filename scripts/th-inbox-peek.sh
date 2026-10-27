#!/usr/bin/env bash
# Read the caller's inbox without acknowledging unless --ack is given.
set -u
script_dir=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
source "$script_dir/th-watch-lib.sh"
ack=0
if [[ ${1:-} == --ack ]]; then ack=1; shift; fi
if (( $# )); then echo 'usage: th-inbox-peek.sh [--ack]' >&2; exit 2; fi
owner=$(project_owner "$PWD") || exit 2
owner=$(realpath -m "$owner") || exit 2
[[ -n $owner && $owner != / ]] || exit 2
python3 "$script_dir/th-inbox-read.py" "$owner" "$ack" peek
rc=$?
(( rc == 1 )) && exit 0
exit "$rc"
