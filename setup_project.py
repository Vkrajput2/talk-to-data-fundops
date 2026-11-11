"""One-time local setup for new contributors.

Run once after cloning:   python setup_project.py        (Windows: py setup_project.py)

What it does:
  1. checks the Python version
  2. creates a virtual environment in .venv
  3. installs the pinned dependencies (requirements-dev.txt)
  4. creates .env from .env.example if it does not exist (you add your own API key)
  5. builds the local SQLite database from the CSVs in Data/
  6. runs the offline test suite (skip with --skip-tests)

It never prints or stores any API key. Safe to re-run.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
MIN_PY = (3, 10)


def step(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


def run(cmd: list[str], **kw) -> None:
    print("   $ " + " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], cwd=ROOT, check=True, **kw)


def venv_python() -> Path:
    sub = "Scripts/python.exe" if sys.platform.startswith("win") else "bin/python"
    return VENV / sub


def main() -> int:
    ap = argparse.ArgumentParser(description="One-time local project setup.")
    ap.add_argument("--skip-tests", action="store_true", help="do not run the offline tests")
    ap.add_argument("--recreate", action="store_true", help="delete and recreate .venv")
    args = ap.parse_args()

    step("Checking Python version")
    if sys.version_info < MIN_PY:
        print(f"   Python {MIN_PY[0]}.{MIN_PY[1]}+ is required, found {sys.version.split()[0]}.")
        return 1
    print(f"   Python {sys.version.split()[0]} OK")

    step("Creating virtual environment (.venv)")
    if args.recreate and VENV.exists():
        shutil.rmtree(VENV)
    if venv_python().exists():
        print("   .venv already exists, reusing it (use --recreate to rebuild)")
    else:
        run([sys.executable, "-m", "venv", VENV])

    py = venv_python()
    step("Installing dependencies")
    run([py, "-m", "pip", "install", "--upgrade", "pip"])
    run([py, "-m", "pip", "install", "-r", "requirements-dev.txt"])

    step("Preparing .env")
    env, example = ROOT / ".env", ROOT / ".env.example"
    if env.exists():
        print("   .env already exists, leaving it unchanged")
    elif example.exists():
        shutil.copyfile(example, env)
        print("   created .env from .env.example")
    else:
        print("   .env.example not found; create .env manually")

    step("Building the local database from Data/*.csv")
    build = (
        "from app.config import load_settings; from app.db import ensure_database; "
        "s = load_settings(); "
        "print('   database rebuilt' if ensure_database(s.data_dir, s.db_path, s.use_supplement) "
        "else '   database already up to date')"
    )
    run([py, "-c", build])

    if args.skip_tests:
        print("\n(skipping tests)")
    else:
        step("Running offline tests")
        try:
            run([py, "-m", "pytest", "-m", "not live", "-q"])
        except subprocess.CalledProcessError:
            print("\n   Some tests failed. Setup is otherwise complete; check the output above.")

    activate = r".venv\Scripts\activate" if sys.platform.startswith("win") else "source .venv/bin/activate"
    print(
        "\nSetup complete. Next steps:\n"
        "  1. Open .env and set LLM_API_KEY (get a key from your LLM provider; never commit .env).\n"
        f"  2. Activate the environment:  {activate}\n"
        "  3. Start the app:  python -m uvicorn app.main:app   then open the address it prints.\n"
        "  4. Optional live check against your model:  python -m pytest -m live\n"
        "See README.md and ARCHITECTURE.md for details."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as e:
        print(f"\nSetup failed while running: {' '.join(map(str, e.cmd))}")
        raise SystemExit(e.returncode)
