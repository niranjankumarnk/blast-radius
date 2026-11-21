"""Check the compile-only verdict for test_execute_tpcds against the full per-query replay, on every generated patch
where the compile-only check applies. Writes SETDIR/cval/<id>.json = {"compile": bool, "full": bool}.
usage: compile_eval.py SETDIR      then: compile_eval.py SETDIR --table"""
import collections, json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
os.environ.update(BR_SRC=os.path.join(ROOT, "sqlglot-src"), BR_MAP=os.path.join(ROOT, "map.bin"), BR_TMP=os.environ.get("BR_TMP", "/tmp")); sys.path.insert(0, ROOT)
d = os.path.join(ROOT, sys.argv[1]); out = os.path.join(d, "cval"); os.makedirs(out, exist_ok=True)
if "--table" not in sys.argv:
    import selector as S
    tests = json.load(open(os.path.join(ROOT, "sample/tests.json")))["tests"]; sel = S.Selector(tests)
    obj = next(S.Units(tests, *u) for u in S.UNIT_TESTS if u[3] == "tpcds"); assert obj.ok and obj.esel is not None
    for f in sorted(os.listdir(d)):
        if not f.endswith(".patch") or os.path.exists(os.path.join(out, f[:-6] + ".json")): continue
        text = open(os.path.join(d, f), encoding="utf-8").read(); b = sel.select(text)
        if b == sel.all or not b >> obj.test & 1: continue
        us = obj.units(text)
        if us is None or any(full for _, full in us): continue
        edits = S.patch_edits(S.parse_patch(text))
        if edits is None: continue
        c, _ = S.run_units(edits, obj, us, 60, time.time() + 900, f[:-6])
        full, _ = S.run_units(edits, obj, [(u, True) for u, _ in us], 60, time.time() + 900, f[:-6] + " full")
        if c is None or full is None: continue
        json.dump({"compile": c, "full": full, "units": len(us)}, open(os.path.join(out, f[:-6] + ".json"), "w"))
tab = collections.Counter(tuple(sorted(json.load(open(os.path.join(out, f))).items())[:2]) for f in os.listdir(out))
for k, v in sorted(tab.items()): print(dict(k), v)
