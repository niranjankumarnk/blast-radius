"""One pytest test per TPC-DS query, for building the per-query coverage map (see tpcds_replay.py)."""
import os, sys, time
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tpcds_replay import Replay

N = int(os.environ.get("TPCDS_N", "0")) or len(Replay().sqls)


@pytest.mark.parametrize("i", range(N))
def test_q(i):
    r = Replay(); t0 = time.process_time()
    digest = r.run(i)
    out = os.environ.get("REPLAY_OUT")
    if out:
        with open(os.path.join(out, f"{i}.txt"), "w") as fh: fh.write(f"{digest} {time.process_time() - t0:.3f}\n")
