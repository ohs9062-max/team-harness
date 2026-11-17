"""Run a Python command with the harness source available, without PYTHONPATH."""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
args = sys.argv[1:]
if not args:
    raise SystemExit("expected a Python command")
if args[0] == "-m":
    sys.argv = args[1:]
    runpy.run_module(sys.argv[0], run_name="__main__", alter_sys=True)
elif args[0] == "-c":
    sys.argv = ["-c", *args[2:]]
    exec(compile(args[1], "<string>", "exec"), {"__name__": "__main__"})
elif args[0] == "-":
    sys.argv = args
    exec(compile(sys.stdin.read(), "<stdin>", "exec"), {"__name__": "__main__"})
else:
    sys.argv = args
    sys.path.insert(0, str(Path(args[0]).resolve().parent))
    runpy.run_path(args[0], run_name="__main__")
