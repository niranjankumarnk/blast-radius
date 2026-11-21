"""Record, at the pinned commit, how each fixture-driven optimizer test calls check_file, and list the units:
one per (test, check_file call, fixture). Run from the pinned checkout:  python build/f/calls.py OUT_DIR

Writes OUT_DIR/calls.pkl (the recorded arguments; they hold functions of the test module, so it is a build
intermediate) and OUT_DIR/fx_meta.json (what the selector needs: tests, fixtures per call, the share of a test's
time spent outside check_file, and the unit list)."""
import ast, concurrent.futures as cf, json, os, pickle, sys, time, unittest


class Inline:
    def __init__(self, *a, **k): pass
    def submit(self, fn, *a, **k):
        f = cf.Future()
        try:
            a, k = pickle.loads(pickle.dumps((a, k))); f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:
            f.set_exception(e)
        return f
    def shutdown(self, *a, **k): pass


if __name__ == "__main__":
    cf.ProcessPoolExecutor = Inline
    sys.path.insert(0, os.getcwd())
    import tests.test_optimizer as M

    out = sys.argv[1]
    tree = ast.parse(open(M.__file__).read())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TestOptimizer")
    names = [n.name for n in cls.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
             and any(isinstance(m, ast.Attribute) and m.attr == "check_file" for m in ast.walk(n))]
    real = M.TestOptimizer.check_file


    def run(name):
        t0 = time.process_time(); res = unittest.TestResult(); unittest.TestSuite([M.TestOptimizer(name)]).run(res)
        assert res.wasSuccessful(), (name, res.errors, res.failures)
        return time.process_time() - t0


    calls, meta, units = {}, {}, []
    for name in names:
        rec = []
        def fake(self, file, func, pretty=False, execute=None, only=None, **kw): rec.append((file, func, pretty, execute, only, kw))
        M.TestOptimizer.check_file = fake; body = run(name); first = list(rec); body = min(body, run(name)); rec = first
        M.TestOptimizer.check_file = real; full = min(run(name), run(name))
        pickle.dumps(rec); calls[name] = rec
        tid = f"tests/test_optimizer.py::TestOptimizer::{name}"
        counts = [sum(1 for _ in M.load_sql_fixture_pairs(f"optimizer/{c[0]}.sql")) for c in rec]
        meta[tid] = {"calls": counts, "files": [f"tests/fixtures/optimizer/{c[0]}.sql" for c in rec], "body": round(min(1.0, body / full), 3), "first": len(units)}
        units += [[tid, c, k] for c, n in enumerate(counts) for k in range(n)]
        print(name, counts, meta[tid]["body"], file=sys.stderr)
    pickle.dump(calls, open(os.path.join(out, "calls.pkl"), "wb"))
    json.dump({"tests": meta, "units": units}, open(os.path.join(out, "fx_meta.json"), "w"))
    print("units", len(units), file=sys.stderr)
