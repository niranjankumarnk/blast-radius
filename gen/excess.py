"""Where does the static selection schedule more than the tests that really fail, once the heavy tests are set aside?
Groups the excess by the selection rule that produced it.   usage: excess.py   (samples and gen/set*/labels)"""
import collections, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
os.environ.update(BR_SRC=os.path.join(ROOT, "sqlglot-src"), BR_MAP=os.path.join(ROOT, "map.bin")); sys.path.insert(0, ROOT)
import selector as S
tests = json.load(open(os.path.join(ROOT, "sample/tests.json")))["tests"]; ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
sel = S.Selector(tests); log = []
def wrap(name, label):
    orig = getattr(S.Selector, name)
    def f(self, *a, **k):
        r = orig(self, *a, **k)
        if isinstance(r, int) and r: log.append((label, r))
        return r
    setattr(S.Selector, name, f)
for n, l in (("class_attr", "class attribute"), ("fn_tests", "lambda or function entry"), ("gone_name", "removed or renamed name"),
             ("readers", "file readers (fixtures)"), ("untraced", "untraced file"), ("file_tests", "whole file (import time, syntax error, add/delete)"), ("cover", "statement coverage")):
    wrap(n, l)
ORDER = ["class attribute", "removed or renamed name", "lambda or function entry", "file readers (fixtures)", "untraced file", "statement coverage", "whole file (import time, syntax error, add/delete)"]
cost = lambda nums: S.pack_cost(sorted(nums), ms) / 1000 if nums else 0.0
def items():
    ans = json.load(open(os.path.join(ROOT, "sample/answers.json")))["commits"]
    for c, a in ans.items(): yield "samples", open(os.path.join(ROOT, "sample/commits", c + ".patch")).read(), set(a["failing"])
    for sd in ("set1", "set2"):
        d = os.path.join(HERE, sd, "labels")
        for f in sorted(os.listdir(d)):
            r = json.load(open(os.path.join(d, f)))
            if not r.get("applied") or "failing" not in r or r.get("crash") or r.get("hang"): continue
            yield "generated", open(os.path.join(HERE, sd, f[:-5] + ".patch"), encoding="utf-8").read(), set(r["failing"])
tab = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0.0, 0.0])); n = collections.Counter(); tot = collections.defaultdict(lambda: [0.0, 0.0])
for src, text, failing in items():
    del log[:]; b = sel.select(text); n[src] += 1
    nums = {i for i in range(b.bit_length()) if b >> i & 1 and ms[i] < S.HEAVY_MS}
    fail = {i for i in failing if ms[i] < S.HEAVY_MS}
    c_sel, c_need = cost(nums), cost(fail & nums); excess = c_sel - c_need
    tot[src][0] += c_sel; tot[src][1] += c_need
    if excess < 0.05: continue
    extra = sum(1 << i for i in nums - fail)
    if b == sel.all: rule = "everything selected (fallback or core import-time change)"
    else:
        best = collections.Counter()
        for label, r in log: best[label] = max(best[label], bin(r & extra).count("1"))
        top = max(best.values(), default=0)
        rule = next((l for l in ORDER if best.get(l) == top and top), "statement coverage")
    kind = "breaks nothing" if not fail else "breaks something"
    t = tab[src][(rule, kind)]; t[0] += 1; t[1] += excess; t[2] += c_sel
for src in tab:
    print(f"== {src}: {n[src]} patches; scheduled without heavy tests {tot[src][0]:.1f} s, needed {tot[src][1]:.1f} s, excess {tot[src][0]-tot[src][1]:.1f} s = {(tot[src][0]-tot[src][1])/n[src]*40:.1f} s per 40 commits")
    for (rule, kind), (k, ex, cs) in sorted(tab[src].items(), key=lambda x: -x[1][1]):
        print(f"   {ex:7.1f} s excess  ({ex/n[src]*40:5.1f} s per 40)  {k:3} patches  {rule}; {kind}")
