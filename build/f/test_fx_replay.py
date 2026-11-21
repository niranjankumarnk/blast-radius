"""One pytest test per (optimizer test, check_file call, fixture), for building the per-fixture coverage map.
Each runs check_file on that one fixture in a fresh process, the worker's part included (run inline)."""
import concurrent.futures as cf, json, os, pickle, sys
import pytest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from calls import Inline  # noqa: E402  (calls.py only records when run as a script)
cf.ProcessPoolExecutor = Inline
import tests.test_optimizer as M  # noqa: E402

D = os.environ.get("FX_DIR", HERE)
META = json.load(open(os.path.join(D, "fx_meta.json"))); CALLS = pickle.load(open(os.path.join(D, "calls.pkl"), "rb"))


@pytest.mark.parametrize("n", range(len(META["units"])))
def test_u(n):
    tid, c, k = META["units"][n]; name = tid.split("::")[-1]
    file, func, pretty, execute, only, kw = CALLS[name][c]
    M.TestOptimizer.setUpClass()
    orig = M.load_sql_fixture_pairs
    M.load_sql_fixture_pairs = lambda f: (p for i, p in enumerate(orig(f)) if i == k)
    try:
        M.TestOptimizer(name).check_file(file, func, pretty=pretty, execute=execute, only=only, **kw)
    finally:
        M.load_sql_fixture_pairs = orig; M.TestOptimizer.conn.close()
