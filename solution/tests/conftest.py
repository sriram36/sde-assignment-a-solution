import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCHEMAS = REPO / "assignment-a-data" / "schemas"
GEN = REPO / "assignment-a-data" / "tools" / "generate_data.py"


@pytest.fixture(scope="session")
def gen_data(tmp_path_factory):
    """seed -> directory with generated small data (cached per session)."""
    cache = {}

    def make(seed: int) -> Path:
        if seed not in cache:
            out = tmp_path_factory.mktemp(f"data{seed}")
            subprocess.run([sys.executable, str(GEN), "--seed", str(seed), "--scale", "small",
                            "--out", str(out)], check=True, capture_output=True)
            cache[seed] = out
        return cache[seed]
    return make
