"""Evidence for the heavy-test rule: when every cheaper selected test passes on a patch, does the heavy test pass too?

For each patch whose static selection carries a heavy test and enough cheaper tests on every changed spot, run the
cheaper tests on the patched tree; if they all pass, run the heavy test (twice if it fails, to rule out a flake).

usage: label_heavy.py SRC PATCHDIR TESTS_JSON [MAX_EST]
Writes PATCHDIR/heavy/<id>.json. Evaluation only: nothing here ships in the image.
"""
import json, os, shutil, subprocess, sys, tempfile

SRC, PD, TESTS = os.path.realpath(sys.argv[1]), os.path.realpath(sys.argv[2]), sys.argv[3]
MAX_EST = float(sys.argv[4]) if len(sys.argv) > 4 else 1e9
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); PY = os.path.join(ROOT, ".venv", "bin", "python")
os.environ.update(BR_SRC=SRC, BR_MAP=os.path.join(ROOT, "map.bin")); sys.path.insert(0, ROOT)
import selector as S

tests = json.load(open(TESTS))["tests"]; ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
tid = {t["number"]: t["id"] for t in tests}; num = {t["id"]: t["number"] for t in tests}
sel = S.Selector(tests)
OUT = os.path.join(PD, "heavy"); os.makedirs(OUT, exist_ok=True)
WORK = tempfile.mkdtemp(prefix="brheavy-")
kinds = json.load(open(os.path.join(PD, "kinds.json"))) if os.path.exists(os.path.join(PD, "kinds.json")) else {}


def plan(text):
    b = sel.select(text)
    if b == sel.all or not b: return None
    nums = [i for i in range(b.bit_length()) if b >> i & 1]
    heavy = [n for n in nums if ms[n] >= S.HEAVY_MS]
    wit = [n for n in nums if ms[n] < S.HEAVY_MS and tid[n].split("::")[0] not in sel.resolvers()]
    wb = sum(1 << n for n in wit)
    if not heavy or not wit or not all(bin(p & wb).count("1") >= S.MIN_WITNESS for p in sel.parts if any(p >> h & 1 for h in heavy)): return None
    est = S.run_estimate(wit, ms, tid)
    saving = (S.pack_cost(nums, ms) - S.pack_cost([n for n in nums if n not in heavy and n not in wit], ms)) / 1000
    return heavy, wit, est, saving, list(sel.parts)


def pytest_ids(d, ids, cpus="1", timeout=3600):
    res = os.path.join(d, ".outcome.json"); argf = os.path.join(d, ".ids.txt")
    if os.path.exists(res): os.remove(res)
    open(argf, "w").write("\n".join(ids))
    env = dict(os.environ, SKIP_INTEGRATION="0", OUTCOME_OUT=res, PYTHONPATH=d + ":" + HERE, PYTHONDONTWRITEBYTECODE="1", PYTHON_CPU_COUNT=cpus,
               OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", PATH=os.path.dirname(PY) + ":" + os.environ["PATH"])
    try:
        subprocess.run([PY, "-m", "pytest", "-p", "outcome_plugin", "-q", "-p", "no:cacheprovider", "--timeout", "900", "@" + argf],
                       cwd=d, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if not os.path.exists(res): return None
    o = json.load(open(res))
    if o["seen"] < len(ids): return None
    bad = set(o["failed"])
    for c in o["collect_errors"]: bad |= {i for i in ids if i.split("::")[0] == c}
    return bad & set(ids)


def run(cid):
    out = os.path.join(OUT, cid + ".json")
    if os.path.exists(out): return "have"
    p = plan(open(os.path.join(PD, cid + ".patch"), encoding="utf-8").read())
    if p is None: return "no-plan"
    heavy, wit, est, saving, sel_parts = p
    if est > MAX_EST: return "too-slow"
    rec = {"kind": kinds.get(cid, ""), "heavy": [tid[h].split("::")[-1] for h in heavy], "witnesses": len(wit), "est": round(est, 1),
           "fires": bool(est <= S.MAX_EST and est <= S.HEAVY_RATIO * saving)}
    d = os.path.join(WORK, cid)
    subprocess.run(["git", "-C", SRC, "worktree", "add", "--detach", "-f", d, "HEAD"], capture_output=True, check=True)
    try:
        if subprocess.run(["git", "-C", d, "apply", "--whitespace=nowarn", os.path.join(PD, cid + ".patch")], capture_output=True).returncode: return "noapply"
        wf = pytest_ids(d, [tid[n] for n in wit])
        if wf is None: return "norun"
        rec["witness_nums"] = wit                                     # (test numbers, as in tests.json)
        rec["witness_failed_nums"] = sorted(num[i] for i in wf); rec["witness_failed"] = len(wf)
        rec["heavy_nums"] = heavy
        rec["parts"] = [[i for i in range(p.bit_length()) if p >> i & 1] for p in sel_parts if any(p >> h & 1 for h in heavy)][:60]
        hf = pytest_ids(d, [tid[h] for h in heavy], cpus="2")          # always: any drop rule can then be checked offline
        if hf is None: return "norun-heavy"
        rec["heavy_failed"] = sorted(i.split("::")[-1] for i in hf)
        if hf and not wf:                                             # the full rule would be wrong here: make sure it is not a flake
            again = pytest_ids(d, sorted(hf), cpus="2")
            rec["heavy_failed_again"] = None if again is None else sorted(i.split("::")[-1] for i in again)
        json.dump(rec, open(out + ".tmp", "w")); os.rename(out + ".tmp", out)
        return "ok"
    finally:
        subprocess.run(["git", "-C", SRC, "worktree", "remove", "--force", d], capture_output=True)


ids = sorted(f[:-6] for f in os.listdir(PD) if f.endswith(".patch"))
pl = os.path.join(PD, "plans.json")
if os.path.exists(pl):                                         # the cases where the selector would really run come first
    fire = set(json.load(open(pl))["fire"]); ids.sort(key=lambda c: (c not in fire, c))
import collections
res = collections.Counter()
for c in ids:
    try: res[run(c)] += 1
    except Exception as e: res[type(e).__name__] += 1
shutil.rmtree(WORK, ignore_errors=True); subprocess.run(["git", "-C", SRC, "worktree", "prune"], capture_output=True)
print(res)
