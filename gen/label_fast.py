"""Fast labelling for patches to files few tests touch: run only the tests that import or read a changed file.

A test that never executes a line of a changed file, never reads it and does not start a subprocess cannot be
affected by the change, so the failing tests are among that superset. Patches whose superset is large are left
to label.py (the whole suite).

usage: label_fast.py SRC PATCHDIR TESTS_JSON [JOBS] [MAX_SECONDS]
"""
import json, os, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor

SRC, PD, TESTS = os.path.realpath(sys.argv[1]), os.path.realpath(sys.argv[2]), sys.argv[3]
JOBS = int(sys.argv[4]) if len(sys.argv) > 4 else 4
MAXS = float(sys.argv[5]) if len(sys.argv) > 5 else 12.0
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); PY = os.path.join(ROOT, ".venv", "bin", "python")
os.environ.update(BR_SRC=SRC, BR_MAP=os.path.join(ROOT, "map.bin")); sys.path.insert(0, ROOT)
import selector as S

tests = json.load(open(TESTS))["tests"]; num = {t["id"]: t["number"] for t in tests}; secs = {t["number"]: t["seconds"] for t in tests}
by_file = {}
for t in tests: by_file.setdefault(t["file"], []).append(t["number"])
sel = S.Selector(tests)
LAB = os.path.join(PD, "labels"); os.makedirs(LAB, exist_ok=True)
WORK = tempfile.mkdtemp(prefix="brfast-")


def superset(text):
    b = sel.subproc
    for f in S.parse_patch(text):
        for p in {f[0], f[1]} - {None}:
            if p.endswith(("__init__.py", "conftest.py")) or f[0] != f[1] or not p.startswith(("sqlglot/", "tests/")): return None
            if p.endswith(".py") and p not in sel.files and p not in sel.by_file: return None
            b |= sel.file_tests(p) | sel.readers(p, True) | sel.by_file.get(p, 0)
    return [i for i in range(b.bit_length()) if b >> i & 1]


def run(cid):
    out = os.path.join(LAB, cid + ".json")
    if os.path.exists(out): return "have"
    nums = superset(open(os.path.join(PD, cid + ".patch"), encoding="utf-8").read())
    if nums is None or sum(secs[n] for n in nums) > MAXS: return "big"
    d = os.path.join(WORK, cid)
    subprocess.run(["git", "-C", SRC, "worktree", "add", "--detach", "-f", d, "HEAD"], capture_output=True, check=True)
    try:
        if subprocess.run(["git", "-C", d, "apply", "--whitespace=nowarn", os.path.join(PD, cid + ".patch")], capture_output=True).returncode:
            json.dump({"applied": False}, open(out, "w")); return "noapply"
        res = os.path.join(d, ".outcome.json"); ids = [tests[n]["id"] for n in nums]
        argf = os.path.join(d, ".ids.txt"); open(argf, "w").write("\n".join(ids))
        env = dict(os.environ, SKIP_INTEGRATION="0", OUTCOME_OUT=res, PYTHONPATH=d + ":" + HERE, PYTHONDONTWRITEBYTECODE="1", PYTHON_CPU_COUNT="1",
                   PATH=os.path.dirname(PY) + ":" + os.environ["PATH"])
        try:
            subprocess.run([PY, "-m", "pytest", "-p", "outcome_plugin", "-q", "-p", "no:cacheprovider", "--timeout", "300", "@" + argf],
                           cwd=d, env=env, capture_output=True, text=True, timeout=1200)
        except subprocess.TimeoutExpired:
            json.dump({"applied": True, "fast": True, "hang": True, "failing": nums}, open(out, "w")); return "hang"
        o = json.load(open(res)) if os.path.exists(res) else None
        if o is None or o["seen"] < len(ids):
            # pytest ran nothing or not everything (a test id that no longer exists stops the whole invocation):
            # run the files that hold the superset instead, and keep the verdicts of the superset's tests.
            if os.path.exists(res): os.remove(res)
            files = sorted({i.split("::")[0] for i in ids if os.path.exists(os.path.join(d, i.split("::")[0]))})
            try:
                subprocess.run([PY, "-m", "pytest", "-p", "outcome_plugin", "-q", "-p", "no:cacheprovider", "--timeout", "300",
                                "--deselect", "tests/test_parser.py::TestParser::test_max_nodes", "--deselect", "tests/test_executor.py::TestExecutor::test_execute_tpcds"] + files,
                               cwd=d, env=env, capture_output=True, text=True, timeout=2400)
            except subprocess.TimeoutExpired:
                return "hang2"
            if not os.path.exists(res): return "norun"                 # no label: the outcome is unknown
            o = json.load(open(res))
            if any(secs[n] >= 20 for n in nums): return "heavy-unknown"   # (the two slowest tests were not run)
        failing = {num[i] for i in o["failed"] if i in num and num[i] in set(nums)}
        for c in o["collect_errors"]: failing.update(n for n in by_file.get(c, []) if n in set(nums))
        json.dump({"applied": True, "fast": True, "failing": sorted(failing), "superset": len(nums), "collect_errors": o["collect_errors"], "seen": o["seen"]}, open(out + ".tmp", "w"))
        os.rename(out + ".tmp", out); return "ok"
    finally:
        subprocess.run(["git", "-C", SRC, "worktree", "remove", "--force", d], capture_output=True)


ids = sorted(f[:-6] for f in os.listdir(PD) if f.endswith(".patch"))
with ThreadPoolExecutor(JOBS) as ex: res = list(ex.map(run, ids))
shutil.rmtree(WORK, ignore_errors=True); subprocess.run(["git", "-C", SRC, "worktree", "prune"], capture_output=True)
import collections; print(collections.Counter(res))
