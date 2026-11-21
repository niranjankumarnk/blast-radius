"""Score the selector on a labelled patch set. usage: evalset.py SETDIR [env KEY=VAL ...]"""
import json, os, resource, subprocess, sys, tempfile, shutil
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sd = os.path.abspath(sys.argv[1])
tests = json.load(open(os.path.join(here, "sample/tests.json")))["tests"]; ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
kinds = json.load(open(os.path.join(sd, "kinds.json"))) if os.path.exists(os.path.join(sd, "kinds.json")) else {}
lab = {}
for f in sorted(os.listdir(os.path.join(sd, "labels"))):
    if f.endswith(".json"):
        d = json.load(open(os.path.join(sd, "labels", f)))
        if d.get("applied"): lab[f[:-5]] = d
tmp = tempfile.mkdtemp(prefix="breval-"); os.mkdir(os.path.join(tmp, "commits"))
for c in lab: shutil.copy(os.path.join(sd, c + ".patch"), os.path.join(tmp, "commits"))
shutil.copy(os.path.join(here, "sample/tests.json"), tmp)
env = dict(os.environ, BR_DATA=tmp, BR_SRC=os.path.join(here, "sqlglot-src"), PATH=os.path.join(here, ".venv/bin") + ":" + os.environ["PATH"])
for kv in sys.argv[2:]: k, v = kv.split("=", 1); env[k] = v
r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
p = subprocess.run([os.path.join(here, ".venv/bin/python"), os.path.join(here, "selector.py")], env=env, capture_output=True)
r1 = resource.getrusage(resource.RUSAGE_CHILDREN); cpu = r1.ru_utime + r1.ru_stime - r0.ru_utime - r0.ru_stime
shutil.rmtree(tmp)
if p.returncode: print(p.stderr.decode()[-3000:]); sys.exit(1)
log = {l.split(":")[0]: l for l in p.stderr.decode().splitlines() if ": ran " in l}
tot = miss = ideal = 0; rows = []
for line in p.stdout.decode().splitlines():
    d = json.loads(line); c = d["commit"]; a = set(lab[c]["failing"]); ws = [w for w in d["workers"] if w]
    sel = {n for w in ws for n in w}; cost = (max(sum(ms[n] for n in w) for w in ws) + 2000 * len(ws)) if ws else 0
    m = sorted(a - sel); best = (max(ms[n] for n in a) + 2000) if a else 0
    if len(a) > 8:      # oracle packs too
        best = min(max(sum(sorted((ms[n] for n in a), reverse=True)[i::k]) for i in range(k)) + 2000 * k for k in range(1, 9))
    tot += cost + (300000 if m else 0); miss += bool(m); ideal += best
    rows.append((c, kinds.get(c, ""), len(a), len(sel), cost / 1000, best / 1000, m, c in log))
def confirm(c, m):
    """Re-run the files of the missed tests on the patched tree, twice: a miss counts only if a missed test fails again."""
    src = os.path.join(here, "sqlglot-src"); w = os.path.join(os.environ.get("EV_WORK", "/private/tmp"), "brconfirm-" + c)
    subprocess.run(["git", "-C", src, "worktree", "add", "--detach", "-f", w, "HEAD"], capture_output=True)
    try:
        if subprocess.run(["git", "-C", w, "apply", "--whitespace=nowarn", os.path.join(sd, c + ".patch")], capture_output=True).returncode: return None
        files = sorted({tests[n]["file"] for n in m}); again = set()
        for _ in range(2):
            res = os.path.join(w, ".outcome.json")
            e = dict(os.environ, SKIP_INTEGRATION="0", OUTCOME_OUT=res, PYTHONPATH=w + ":" + os.path.join(here, "gen"), PYTHONDONTWRITEBYTECODE="1", PYTHON_CPU_COUNT="2",
                     PATH=os.path.join(here, ".venv/bin") + ":" + os.environ["PATH"])
            if os.path.exists(res): os.remove(res)
            subprocess.run([os.path.join(here, ".venv/bin/python"), "-m", "pytest", "-p", "outcome_plugin", "-q", "-p", "no:cacheprovider", "--timeout", "900",
                            "--deselect", "tests/test_parser.py::TestParser::test_max_nodes", "--deselect", "tests/test_executor.py::TestExecutor::test_execute_tpcds"] + files,
                           cwd=w, env=e, capture_output=True)
            if not os.path.exists(res): return None
            o = json.load(open(res)); bad = set(o["failed"])
            for ce in o["collect_errors"]: bad |= {t["id"] for t in tests if t["file"] == ce}
            again |= {n for n in m if tests[n]["id"] in bad}
        return sorted(again)
    finally:
        subprocess.run(["git", "-C", src, "worktree", "remove", "--force", w], capture_output=True)


real = 0
for c, k, na, ns, cost, best, m, ran in rows:
    if m and "CONFIRM" in os.environ:
        heavy = [n for n in m if tests[n]["seconds"] >= 20]
        again = confirm(c, [n for n in m if n not in heavy])
        verdict = "unconfirmed (could not re-run)" if again is None else ("CONFIRMED " + str([tests[n]["id"].split("::")[-1] for n in again[:4]]) if again else "not reproduced: flaky label") + (" [heavy test not re-run]" if heavy else "")
        real += bool(again or again is None or heavy)
        print(f"  {c}: {verdict}")
for c, k, na, ns, cost, best, m, ran in rows:
    if m: print(f"MISS {c} {k[:60]:60} fail={na} sel={ns} ran={ran} missed={[tests[n]['id'].split('::')[-1] for n in m[:3]]}{'...' if len(m) > 3 else ''}")
if "-v" in os.environ.get("EV", ""):
    for c, k, na, ns, cost, best, m, ran in rows: print(f"{c} {k[:50]:50} fail={na:4} sel={ns:4} cost={cost:6.1f} ideal={best:6.1f} {'ran' if ran else ''}")
if "CONFIRM" in os.environ: print(f"misses reproduced or unverifiable: {real} of {miss}")
print(f"{len(rows)} commits  simulated {tot/1000:.1f}s  misses {miss}  cpu {cpu:.1f}s  ideal {ideal/1000:.1f}s  nonmiss-sim {(tot-300000*miss)/1000:.1f}  ran {sum(r[7] for r in rows)}")
