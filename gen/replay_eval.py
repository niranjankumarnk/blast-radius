"""Does the per-query replay of test_execute_tpcds agree with the real test? For every patch with a known verdict
for that test (gen/set*/heavy, samples), replay all queries on the patched tree, on the full data and on samples
of it, and compare with the pinned commit's digests.

usage: replay_eval.py SETDIR|samples [SAMPLES e.g. 1,4,10]
Writes SETDIR/replay/<id>.json. Evaluation only.
"""
import json, os, subprocess, sys, tempfile, shutil
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); SRC = os.path.join(ROOT, "sqlglot-src"); PY = os.path.join(ROOT, ".venv", "bin", "python")
target = sys.argv[1]; samples = [int(x) for x in (sys.argv[2] if len(sys.argv) > 2 else "1,4,10").split(",")]
REAL = "test_execute_tpcds"
CODE = r'''
import json, sys, time
sys.path.insert(0, sys.argv[1]); sys.path.insert(1, sys.argv[2])
from tpcds_replay import Replay
out = {}
for k in json.loads(sys.argv[3]):
    t0 = time.process_time(); d = []
    try:
        r = Replay(k)
        for i in range(len(r.sqls)):
            try: d.append(r.run(i))
            except BaseException as e: d.append("ERR " + type(e).__name__)
    except BaseException as e:
        d = ["IMPORT " + type(e).__name__]
    out[str(k)] = {"digest": d, "cpu": round(time.process_time() - t0, 2)}
print(json.dumps(out))
'''
def replay(tree):
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    p = subprocess.run([PY, "-c", CODE, tree, ROOT, json.dumps(samples)], capture_output=True, text=True, env=env, timeout=1800)
    return json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 and p.stdout.strip() else None
pinned_path = os.path.join(HERE, "replay_pinned.json")
pinned = json.load(open(pinned_path)) if os.path.exists(pinned_path) else {}
if any(str(k) not in pinned for k in samples):
    pinned.update(replay(SRC)); json.dump(pinned, open(pinned_path, "w"))
items = []
if target == "samples":
    ans = json.load(open(os.path.join(ROOT, "sample/answers.json")))["commits"]; tests = json.load(open(os.path.join(ROOT, "sample/tests.json")))["tests"]
    real = next(t["number"] for t in tests if t["id"].endswith(REAL))
    for c in ("2haz", "2jrq", "3utz", "66ks"): items.append((c, os.path.join(ROOT, "sample/commits", c + ".patch"), real in ans[c]["failing"]))
    outdir = os.path.join(HERE, "sample_replay")
else:
    d = os.path.join(ROOT, target); outdir = os.path.join(d, "replay")
    for f in sorted(os.listdir(os.path.join(d, "heavy"))):
        r = json.load(open(os.path.join(d, "heavy", f)))
        if REAL in r.get("heavy", []) and "heavy_failed" in r:
            fails = REAL in r["heavy_failed"] and not (r.get("heavy_failed_again") == [] and not r.get("witness_failed"))
            items.append((f[:-5], os.path.join(d, f[:-5] + ".patch"), fails))
os.makedirs(outdir, exist_ok=True)
work = tempfile.mkdtemp(prefix="brreplay-")
for cid, patch, fails in items:
    out = os.path.join(outdir, cid + ".json")
    if os.path.exists(out): continue
    w = os.path.join(work, cid)
    subprocess.run(["git", "-C", SRC, "worktree", "add", "--detach", "-f", w, "HEAD"], capture_output=True, check=True)
    try:
        if subprocess.run(["git", "-C", w, "apply", "--whitespace=nowarn", patch], capture_output=True).returncode: continue
        r = replay(w)
        if r is None: continue
        rec = {"tpcds_fails": fails}
        for k in samples:
            a, b = pinned[str(k)]["digest"], r[str(k)]["digest"]
            rec[f"same_{k}"] = a == b; rec[f"cpu_{k}"] = r[str(k)]["cpu"]; rec[f"differing_{k}"] = sum(1 for x, y in zip(a, b) if x != y) if len(a) == len(b) else -1
        json.dump(rec, open(out, "w"))
    finally:
        subprocess.run(["git", "-C", SRC, "worktree", "remove", "--force", w], capture_output=True)
shutil.rmtree(work, ignore_errors=True); subprocess.run(["git", "-C", SRC, "worktree", "prune"], capture_output=True)
# summary
import collections
tab = collections.Counter()
for f in sorted(os.listdir(outdir)):
    r = json.load(open(os.path.join(outdir, f)))
    for k in samples:
        if f"same_{k}" in r: tab[(k, "replay same" if r[f"same_{k}"] else "replay differs", "test FAILS" if r["tpcds_fails"] else "test passes")] += 1
for key in sorted(tab): print(key, tab[key])
