"""Check the exact unit-by-unit verdict for a heavy test against labels: "cannot fail" must never meet a real failure.

usage: units_eval.py gm|tpcds SETDIR...     (uses labels in SETDIR/labels and SETDIR/heavy)
"""
import collections, json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
os.environ.update(BR_SRC=os.path.join(ROOT, "sqlglot-src"), BR_MAP=os.path.join(ROOT, "map.bin"), BR_TMP=os.environ.get("BR_TMP", "/tmp")); sys.path.insert(0, ROOT)
import selector as S
kind = sys.argv[1]
tests = json.load(open(os.path.join(ROOT, "sample/tests.json")))["tests"]
sel = S.Selector(tests)
obj = next(S.Units(tests, *u) for u in S.UNIT_TESTS if u[3] == kind)
assert obj.ok
tab = collections.Counter(); wrong = []; cpu = []
def truth_items():
    ans = json.load(open(os.path.join(ROOT, "sample/answers.json")))["commits"]
    for c, a in ans.items(): yield "sample", c, os.path.join(ROOT, "sample/commits", c + ".patch"), set(a["failing"])
    for sd in sys.argv[2:]:
        d = os.path.join(ROOT, sd); done = set()
        for sub in ("heavy", "labels"):
            if not os.path.isdir(os.path.join(d, sub)): continue
            for f in sorted(os.listdir(os.path.join(d, sub))):
                if f[:-5] in done: continue
                r = json.load(open(os.path.join(d, sub, f)))
                if sub == "heavy":
                    if "heavy_nums" not in r: continue
                    names = {t["number"]: t["id"].split("::")[-1] for t in tests}
                    failing = set(r["witness_failed_nums"]) | {h for h in r["heavy_nums"] if names[h] in r["heavy_failed"]}
                    if obj.test not in r["heavy_nums"]: continue
                else:
                    if not r.get("applied") or "failing" not in r or r.get("crash") or r.get("hang") or r.get("heavy_skipped"): continue
                    failing = set(r["failing"])
                done.add(f[:-5]); yield sd, f[:-5], os.path.join(d, f[:-5] + ".patch"), failing
for src, cid, path, failing in truth_items():
    text = open(path, encoding="utf-8").read(); b = sel.select(text)
    if b == sel.all or not b >> obj.test & 1: continue
    us = obj.units(text); fails = obj.test in failing
    if us is None:
        tab[("cannot be judged by units", "FAILS" if fails else "passes")] += 1; continue
    edits = S.patch_edits(S.parse_patch(text))
    if edits is None: continue
    t0 = time.time(); same, bad = S.run_units(edits, obj, us, obj.estimate(us), time.time() + 600, cid)
    tab[("no answer" if same is None else "replay: cannot fail" if same else "replay: a unit differs", "FAILS" if fails else "passes", )] += 1
    if same and fails: wrong.append((src, cid))
for k in sorted(tab): print(k, tab[k])
print("WRONG (replay says cannot fail, the test fails):", wrong)
