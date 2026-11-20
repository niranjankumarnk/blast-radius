"""Local eval: run selector natively on sample/, score with the kit's score.py, print per-commit detail."""
import json, os, resource, subprocess, sys, time
here = os.path.dirname(os.path.abspath(__file__))
env = dict(os.environ, BR_DATA=os.path.join(here, "sample"), BR_SRC=os.path.join(here, "sqlglot-src"))
t0 = time.time(); r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
p = subprocess.run([sys.executable, os.path.join(here, sys.argv[1] if len(sys.argv) > 1 else "selector.py")], env=env, capture_output=True)
r1 = resource.getrusage(resource.RUSAGE_CHILDREN); cpu = (r1.ru_utime + r1.ru_stime) - (r0.ru_utime + r0.ru_stime)
open(os.path.join(here, "output.jsonl"), "wb").write(p.stdout)
if p.returncode: print(p.stderr.decode()[-2000:]); sys.exit(1)
tests = json.load(open(os.path.join(here, "sample/tests.json")))["tests"]; ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
ans = json.load(open(os.path.join(here, "sample/answers.json")))["commits"]
tot = 0; misses = 0; ideal = 0
print(f"{'commit':6} {'kind':8} {'fail':>4} {'sel':>5} {'cost_s':>7} {'ideal':>6}  note")
for line in p.stdout.decode().splitlines():
    d = json.loads(line); a = ans[d["commit"]]; ws = [w for w in d["workers"] if w]
    sel = {n for w in ws for n in w}; cost = (max(sum(ms[n] for n in w) for w in ws) + 2000 * len(ws)) if ws else 0
    miss = sorted(set(a["failing"]) - sel)
    best = (max(ms[n] for n in a["failing"]) + 2000) if a["failing"] else 0
    tot += cost + (300000 if miss else 0); misses += bool(miss); ideal += best
    note = ("MISS " + ",".join(tests[n]["id"].split("::")[-1] for n in miss[:4])) if miss else ("over" if len(sel) > 5 * max(1, len(a["failing"])) and cost > best + 1000 else "")
    print(f"{d['commit']:6} {a['kind']:8} {len(a['failing']):4} {len(sel):5} {cost/1000:7.1f} {best/1000:6.1f}  {note}")
print(f"simulated {tot/1000:.1f}s  misses {misses}  cpu {cpu:.2f}s wall {time.time()-t0:.2f}s  total {tot/1000+cpu:.1f}  (ideal sim {ideal/1000:.1f})")
subprocess.run([sys.executable, os.path.join(here, "score.py"), os.path.join(here, "sample"), os.path.join(here, "sample/answers.json"), os.path.join(here, "output.jsonl"), "--cpu-seconds", f"{cpu:.3f}"])
