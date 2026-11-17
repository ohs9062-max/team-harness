#!/bin/sh
# Start harness Python with its own import path and no inherited Python settings.
root=$(CDPATH= cd -- "$(dirname -- "$(readlink -f -- "$0")")/.." && pwd)
# Retain caller settings for explicitly user-owned verification subprocesses.
if [ "${TH_INTERNAL_ENV_SAVED-}" != 1 ]; then
  for name in PYTHONPATH PYTHONHOME VIRTUAL_ENV PYTHONNOUSERSITE; do
    eval 'present=${'"$name"'+yes}'
    eval 'value=${'"$name"'-}'
    export "TH_INTERNAL_${name}_PRESENT=$present" "TH_INTERNAL_${name}_VALUE=$value"
  done
  export TH_INTERNAL_ENV_SAVED=1
fi
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV
export PYTHONNOUSERSITE=1
if [ -x "$root/.venv/bin/python" ]; then
  exec "$root/.venv/bin/python" "$root/scripts/th-python-bootstrap.py" "$@"
elif command -v uv >/dev/null 2>&1; then
  exec uv run --project "$root" python "$root/scripts/th-python-bootstrap.py" "$@"
else
  exec python3 "$root/scripts/th-python-bootstrap.py" "$@"
fi
