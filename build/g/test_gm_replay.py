"""One pytest test per dialect, for building the per-dialect coverage map of test_generator_memory (see gm_replay.py)."""
import os, sys
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import gm_replay

N = int(os.environ.get("UNITS_N", "0")) or len(gm_replay.names())


@pytest.mark.parametrize("i", range(N))
def test_d(i):
    assert gm_replay.run(gm_replay.names()[i]) < 1024
