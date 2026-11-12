# Shim opt-in deployment

- Global apply installs shim links and PATH blocks only when `~/.team-harness/shim/enabled` exists. `th-sync.sh --enable-shim` enables and installs; `--remove-shim` removes the marker and managed installation. Status reports 켜짐/꺼짐.
- Both shims bound result-file reads with timeout, including FIFO replacement. Existing shim installations are left intact when the marker is absent.
- Temporary-home lifecycle and FIFO regression tests passed (60 deployment and shim tests).
- `uv run --extra dev ruff check src/ -q`: passed.
- `uv run --extra dev ruff format --check src/ -q`: passed.
- `uv run --extra dev pyright src/`: 0 errors, 0 warnings.
- `env PYTHONPATH= uv run --extra dev pytest src/tests/ -q -x -p no:cacheprovider`: 1734 passed, 14 warnings.
