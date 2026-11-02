"""Install the pinned Graphify CLI into its own virtual environment (ADR-0003).

    python -m tools.install_graphify [--dir .graphify-venv]

The environment is separate from TraceMind's. Installation uses `--require-hashes` against
infra/graphify/requirements.lock, so every package (Graphify and its dependencies) must match a
recorded SHA-256 or the install fails. Afterwards set TRACEMIND_GRAPHIFY_BIN to the printed path.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import venv
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "infra" / "graphify" / "requirements.lock"


def bin_path(env_dir: Path) -> Path:
    scripts = "Scripts" if os.name == "nt" else "bin"
    return env_dir / scripts / ("graphify.exe" if os.name == "nt" else "graphify")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument("--dir", default=str(ROOT / ".graphify-venv"))
    args = parser.parse_args(argv)
    env_dir = Path(args.dir).resolve()
    if not LOCK.exists():
        print(f"missing {LOCK}", file=sys.stderr)
        return 2
    venv.EnvBuilder(with_pip=True, clear=False).create(env_dir)
    python = (
        env_dir
        / ("Scripts" if os.name == "nt" else "bin")
        / ("python.exe" if os.name == "nt" else "python")
    )
    cmd = [str(python), "-m", "pip", "install", "--require-hashes", "--no-input", "-r", str(LOCK)]
    result = subprocess.run(cmd, check=False)  # noqa: S603 - fixed argv
    if result.returncode != 0:
        print("hash-verified installation failed", file=sys.stderr)
        return result.returncode
    print(f"TRACEMIND_GRAPHIFY_BIN={bin_path(env_dir)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
