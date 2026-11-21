"""Evidence for the cheaper heavy-test rule: when every selected test outside test_optimizer.py / test_executor.py
passes on a patch, does the heavy test pass? Faster than label_heavy.py: it runs only those tests, and takes the
verdict of test_execute_tpcds from the per-query replay (tpcds_replay.py), which agreed with the real test on every
case checked by replay_eval.py.

usage: label_h2.py SRC PATCHDIR TESTS_JSON
Writes PATCHDIR/h2/<id>.json. Evaluation only: nothing here ships in the image.
"""
import json, os, shutil, subprocess, sys, tempfile

SRC, PD, TESTS = os.path.realpath(sys.argv[1]), os.path.realpath(sys.argv[2]), sys.argv[3]
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); PY = os.path.join(ROOT, ".venv", "bin", "python")
os.environ.update(BR_SRC=SRC, BR_MAP=os.path.join(ROOT, "map.bin")); sys.path.insert(0, ROOT)
import selector as S

tests = json.load(open(TESTS))["tests"]; ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
tid = {t["number"]: t["id"] for t in tests}; num = {t["id"]: t["number"] for t in tests}
sel = S.Selector(tests)
OUT = os.path.join(PD, "h2"); os.makedirs(OUT, exist_ok=True)
WORK = tempfile.mkdtemp(prefix="brh2-")
kinds = json.load(open(os.path.join(PD, "kinds.json"))) if os.path.exists(os.path.join(PD, "kinds.json")) else {}
TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
REPLAY = r'''
import json, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(1, sys.argv[2])
d = []
try:
    from tpcds_replay import Replay
    r = Replay()
    for i in range(len(r.sqls)):
        try: d.append(r.run(i))
        except BaseException as e: d.append("ERR " + type(e).__name__)
except BaseException as e:
    d = ["IMPORT " + type(e).__name__]
print(json.dumps(d))
'''


def replay(tree):
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1"); env.pop("PYTHONPATH", None)
    p = subprocess.run([PY, "-c", REPLAY, tree, ROOT], capture_output=True, text=True, env=env, timeout=1800)
    return json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 and p.stdout.strip() else None


def pytest_ids(d, ids, cpus="1"):
    res = os.path.join(d, ".outcome.json"); argf = os.path.join(d, ".ids.txt")
    if os.path.exists(res): os.remove(res)
    open(argf, "w").write("\n".join(ids))
    env = dict(os.environ, SKIP_INTEGRATION="0", OUTCOME_OUT=res, PYTHONPATH=d + ":" + HERE, PYTHONDONTWRITEBYTECODE="1", PYTHON_CPU_COUNT=cpus,
               OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", PATH=os.path.dirname(PY) + ":" + os.environ["PATH"])
    try:
        subprocess.run([PY, "-m", "pytest", "-p", "outcome_plugin", "-q", "-p", "no:cacheprovider", "--timeout", "900", "@" + argf],
                       cwd=d, env=env, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        return None
    if not os.path.exists(res): return None
    o = json.load(open(res))
    if o["seen"] < len(ids): return None
    bad = set(o["failed"])
    for c in o["collect_errors"]: bad |= {i for i in ids if i.split("::")[0] == c}
    return bad & set(ids)


pinned = replay(SRC)
ids = sorted(json.load(open(os.path.join(PD, "plans.json")))["fire_nonpool"])
import collections
res = collections.Counter()
for cid in ids:
    out = os.path.join(OUT, cid + ".json")
    if os.path.exists(out): res["have"] += 1; continue
    try:
        text = open(os.path.join(PD, cid + ".patch"), encoding="utf-8").read()
        b = sel.select(text)
        nums = [i for i in range(b.bit_length()) if b >> i & 1]
        heavy = [n for n in nums if ms[n] >= S.HEAVY_MS]
        wit = [n for n in nums if ms[n] < S.HEAVY_MS and tid[n].split("::")[0] not in sel.resolvers() and tid[n].split("::")[0] not in S.POOL_FILES]
        parts = [[i for i in range(p.bit_length()) if p >> i & 1] for p in sel.parts if any(p >> h & 1 for h in heavy)]
        if not heavy or not wit or b == sel.all: res["no-plan"] += 1; continue
        d = os.path.join(WORK, cid)
        subprocess.run(["git", "-C", SRC, "worktree", "add", "--detach", "-f", d, "HEAD"], capture_output=True, check=True)
        try:
            if subprocess.run(["git", "-C", d, "apply", "--whitespace=nowarn", os.path.join(PD, cid + ".patch")], capture_output=True).returncode: res["noapply"] += 1; continue
            wf = pytest_ids(d, [tid[n] for n in wit])
            if wf is None: res["norun"] += 1; continue
            rec = {"kind": kinds.get(cid, ""), "heavy_nums": heavy, "witness_nums": wit, "witness_failed_nums": sorted(num[i] for i in wf), "parts": parts[:60], "heavy_failed_nums": []}
            for h in heavy:
                if tid[h] == TPCDS:
                    r = replay(d)
                    if r is None: raise RuntimeError("replay")
                    rec["replay_differing"] = sum(1 for x, y in zip(pinned, r) if x != y) if len(r) == len(pinned) else -1
                    if r != pinned: rec["heavy_failed_nums"].append(h)
                else:
                    hf = pytest_ids(d, [tid[h]], cpus="2")
                    if hf is None: raise RuntimeError("heavy")
                    if hf: rec["heavy_failed_nums"].append(h)
            json.dump(rec, open(out + ".tmp", "w")); os.rename(out + ".tmp", out); res["ok"] += 1
        finally:
            subprocess.run(["git", "-C", SRC, "worktree", "remove", "--force", d], capture_output=True)
    except Exception as e:
        res[type(e).__name__] += 1
shutil.rmtree(WORK, ignore_errors=True); subprocess.run(["git", "-C", SRC, "worktree", "prune"], capture_output=True)
print(res)
