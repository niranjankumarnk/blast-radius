"""Label patches with the tests they really break: apply each to a clean copy of the pinned checkout and run the suite.

usage: label.py SRC PATCHDIR TESTS_JSON [JOBS]
Writes PATCHDIR/labels/<id>.json = {"failing": [numbers], "applied": bool, ...}. Evaluation only.
"""
import json, os, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor

SRC, PD, TESTS = os.path.realpath(sys.argv[1]), os.path.realpath(sys.argv[2]), sys.argv[3]
JOBS = int(sys.argv[4]) if len(sys.argv) > 4 else 6
HERE = os.path.dirname(os.path.abspath(__file__)); PY = os.path.join(os.path.dirname(HERE), ".venv", "bin", "python")
tests = json.load(open(TESTS))["tests"]; num = {t["id"]: t["number"] for t in tests}
by_file = {}
for t in tests: by_file.setdefault(t["file"], []).append(t["number"])
LAB = os.path.join(PD, "labels"); os.makedirs(LAB, exist_ok=True)
WORK = tempfile.mkdtemp(prefix="brlabel-")


def run(cid):
    out = os.path.join(LAB, cid + ".json")
    if os.path.exists(out): return
    d = os.path.join(WORK, cid)
    subprocess.run(["git", "-C", SRC, "worktree", "add", "--detach", "-f", d, "HEAD"], capture_output=True, check=True)
    try:
        a = subprocess.run(["git", "-C", d, "apply", "--whitespace=nowarn", os.path.join(PD, cid + ".patch")], capture_output=True, text=True)
        if a.returncode:
            json.dump({"applied": False, "err": a.stderr[-300:]}, open(out, "w")); return
        res = os.path.join(d, ".outcome.json")
        env = dict(os.environ, SKIP_INTEGRATION="0", OUTCOME_OUT=res, PYTHONPATH=d + ":" + HERE, PYTHONDONTWRITEBYTECODE="1", PYTHON_CPU_COUNT="2",
                   PATH=os.path.dirname(PY) + ":" + os.environ["PATH"])
        try:
            r = subprocess.run([PY, "-m", "pytest", "-p", "outcome_plugin", "-q", "-p", "no:cacheprovider", "--timeout", "120"],
                               cwd=d, env=env, capture_output=True, text=True, timeout=1500)
            tail = r.stdout[-600:]
        except subprocess.TimeoutExpired:
            json.dump({"applied": True, "hang": True, "failing": sorted(num.values())}, open(out, "w")); return
        if not os.path.exists(res):          # pytest died before the session finished (e.g. conftest raised): everything fails
            json.dump({"applied": True, "crash": True, "failing": sorted(num.values()), "tail": tail}, open(out, "w")); return
        o = json.load(open(res)); failing = {num[i] for i in o["failed"] if i in num}
        for c in o["collect_errors"]:        # a test file that cannot be imported fails all of its tests
            failing.update(by_file.get(c, []))
            if c in ("", "tests", ".") or c.endswith("conftest.py") or os.path.isdir(os.path.join(d, c)): failing.update(num.values())
        extra = sorted(i for i in o["failed"] if i not in num)
        json.dump({"applied": True, "failing": sorted(failing), "collect_errors": o["collect_errors"], "seen": o["seen"], "unnumbered_failed": extra[:5]}, open(out + ".tmp", "w"))
        os.rename(out + ".tmp", out)
    finally:
        subprocess.run(["git", "-C", SRC, "worktree", "remove", "--force", d], capture_output=True)


ids = sorted(f[:-6] for f in os.listdir(PD) if f.endswith(".patch"))
with ThreadPoolExecutor(JOBS) as ex: list(ex.map(run, ids))
shutil.rmtree(WORK, ignore_errors=True)
subprocess.run(["git", "-C", SRC, "worktree", "prune"], capture_output=True)
print("labelled", len(os.listdir(LAB)))
