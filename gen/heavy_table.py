"""Evidence table for dropping a heavy test: for each rule, how often it would drop and how often that is wrong.

Rules (a drop needs at least three passing witnesses on every changed spot that selected the heavy test):
  all      every cheaper selected test passes
  nonpool  every cheaper selected test outside test_optimizer.py / test_executor.py passes (those two need pandas,
           duckdb and worker processes, so they are the expensive ones to run)
  min3     the three cheapest witnesses of each changed spot pass (non-pool tests first)

usage: heavy_table.py   (reads sample/, gen/set*/labels, gen/set*/heavy)
"""
import collections, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
os.environ.update(BR_SRC=os.path.join(ROOT, "sqlglot-src"), BR_MAP=os.path.join(ROOT, "map.bin")); sys.path.insert(0, ROOT)
import selector as S
tests = json.load(open(os.path.join(ROOT, "sample/tests.json")))["tests"]; ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
tid = {t["number"]: t["id"] for t in tests}; name = {t["number"]: t["id"].split("::")[-1] for t in tests}
sel = S.Selector(tests)
pool = lambda n: tid[n].split("::")[0] in S.POOL_FILES


def cases():
    """-> (source, id, kind, heavy numbers, witness numbers, parts as sets, failing set or (witness failing, heavy failing))"""
    def static(text):
        b = sel.select(text)
        if b == sel.all or not b: return None
        nums = [i for i in range(b.bit_length()) if b >> i & 1]
        heavy = [n for n in nums if ms[n] >= S.HEAVY_MS]
        wit = [n for n in nums if ms[n] < S.HEAVY_MS and tid[n].split("::")[0] not in sel.resolvers()]
        parts = [{i for i in range(p.bit_length()) if p >> i & 1} for p in sel.parts if any(p >> h & 1 for h in heavy)]
        return (heavy, wit, parts) if heavy else None
    ans = json.load(open(os.path.join(ROOT, "sample/answers.json")))["commits"]
    for c, a in ans.items():
        st = static(open(os.path.join(ROOT, "sample/commits", c + ".patch")).read())
        if st: yield ("sample", c, a["kind"]) + st + (set(a["failing"]),)
    for sd in sorted(os.listdir(HERE)):
        d = os.path.join(HERE, sd)
        if not sd.startswith("set") or not os.path.isdir(d): continue
        kinds = json.load(open(os.path.join(d, "kinds.json"))) if os.path.exists(os.path.join(d, "kinds.json")) else {}
        seen = set()
        if os.path.isdir(os.path.join(d, "heavy")):
            for f in sorted(os.listdir(os.path.join(d, "heavy"))):
                r = json.load(open(os.path.join(d, "heavy", f)))
                if "heavy_nums" not in r: continue
                if r.get("heavy_failed") and not r.get("witness_failed") and r.get("heavy_failed_again") == []: r["heavy_failed"] = []   # a flake
                hf = {h for h in r["heavy_nums"] if name[h] in r["heavy_failed"]}
                seen.add(f[:-5])
                yield (sd, f[:-5], r["kind"], r["heavy_nums"], r["witness_nums"], [set(p) for p in r["parts"]], set(r["witness_failed_nums"]) | hf)
        if os.path.isdir(os.path.join(d, "h2")):               # non-pool witnesses only: evidence for the "nonpool" rule alone
            for f in sorted(os.listdir(os.path.join(d, "h2"))):
                if f[:-5] in seen: continue
                r = json.load(open(os.path.join(d, "h2", f))); seen.add(f[:-5])
                yield (sd + ":nonpool-only", f[:-5], r["kind"], r["heavy_nums"], r["witness_nums"], [set(p) for p in r["parts"]], set(r["witness_failed_nums"]) | set(r["heavy_failed_nums"]))
        if os.path.isdir(os.path.join(d, "labels")):
            for f in sorted(os.listdir(os.path.join(d, "labels"))):
                c = f[:-5]
                if c in seen: continue
                r = json.load(open(os.path.join(d, "labels", f)))
                if not r.get("applied") or "failing" not in r or r.get("crash") or r.get("hang") or r.get("heavy_skipped"): continue
                st = static(open(os.path.join(d, c + ".patch"), encoding="utf-8").read())
                if st: yield (sd, c, kinds.get(c, "")) + st + (set(r["failing"]),)


def witnesses(rule, wit, parts, heavy):
    """The tests the rule runs, or None when it cannot give three witnesses to every changed spot."""
    hp = [p for p in parts if any(h in p for h in heavy)]
    if rule == "all": W = set(wit)
    elif rule == "nonpool": W = {n for n in wit if not pool(n)}
    else:
        W = set()
        for p in hp:
            c = sorted((n for n in wit if n in p), key=lambda n: (pool(n), ms[n]))
            W |= set(c[:S.MIN_WITNESS])
    if not W or not all(len(p & W) >= S.MIN_WITNESS for p in hp): return None
    return W


tab = collections.defaultdict(lambda: collections.Counter()); wrong = []; est = collections.defaultdict(list)
for src, cid, kind, heavy, wit, parts, failing in cases():
    for rule in (("nonpool",) if src.endswith(":nonpool-only") else ("all", "nonpool", "min3")):
        W = witnesses(rule, wit, parts, heavy)
        for h in heavy:
            key = (name[h], rule)
            tab[key]["selected"] += 1
            if W is None: continue
            tab[key]["rule applies"] += 1
            if W & failing:
                tab[key]["a witness fails: kept"] += 1; continue
            tab[key]["dropped"] += 1; est[key].append(S.run_estimate(sorted(W), ms, tid))
            if h in failing:
                tab[key]["WRONG"] += 1; wrong.append((src, cid, kind, name[h], rule, len(W)))
print(f"{'heavy test':24}{'rule':9}{'selected':>9}{'applies':>9}{'kept':>7}{'dropped':>9}{'wrong':>7}  median est CPU of a dropping run")
for key in sorted(tab):
    t = tab[key]; e = sorted(est[key])
    print(f"{key[0]:24}{key[1]:9}{t['selected']:9}{t['rule applies']:9}{t['a witness fails: kept']:7}{t['dropped']:9}{t['WRONG']:7}  {e[len(e)//2]:.1f} s" if e else f"{key[0]:24}{key[1]:9}{t['selected']:9}{t['rule applies']:9}{t['a witness fails: kept']:7}{t['dropped']:9}{t['WRONG']:7}")
for w in wrong: print("WRONG DROP:", w)
