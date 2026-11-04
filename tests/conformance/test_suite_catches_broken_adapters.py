"""The suite's own test: run it with deliberately broken adapters and check each one fails.

Runs the conformance file in a subprocess with `CONFORMANCE_EXTRA` pointing at `broken.py`, so
the broken adapters go through exactly the code path a new adapter would.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from tests.conformance.broken import BROKEN

SUITE = Path(__file__).with_name("test_venue_conformance.py")
ROOT = SUITE.parents[2]


def test_every_broken_adapter_fails_the_test_for_its_rule() -> None:
    env = dict(os.environ, CONFORMANCE_EXTRA="tests.conformance.broken:FACTORIES")
    run = subprocess.run(
        [sys.executable, "-m", "pytest", str(SUITE), "-rf", "-p", "no:cacheprovider"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    failed = set(re.findall(r"^FAILED \S+::(test_\w+)\[([\w-]+)\]", run.stdout, re.MULTILINE))
    assert run.returncode == 1, run.stdout[-2000:]
    for key, (_, test) in BROKEN.items():
        assert (test, key) in failed, f"{key} passed {test}: the suite misses this break"
    # and nothing else fails: the real adapters are unaffected by the broken ones
    assert {key for _, key in failed} == set(BROKEN), sorted(failed)
