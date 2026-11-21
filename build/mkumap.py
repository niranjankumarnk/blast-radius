"""Build a unit map: the same kind of map as map.bin, but its "tests" are the units of one heavy test (the 99
per-query replays of test_execute_tpcds, the 33 per-dialect replays of test_generator_memory) plus one pseudo-test,
"harness".

"harness" stands for everything the real test executes that no replay does (its own body, the class set-up, the
process pool): every line, file read, function entry and class instantiation that map.bin records for the real
test and no replay shows. A patch that selects "harness" cannot be judged unit by unit.

usage: mkumap.py MAP_BIN UNIT_DIR REAL_TEST_ID PREFIX OUT
       (after iso.py / isofn.py have run on UNIT_DIR/tests.json, which lists one pytest id per unit as
        {"tests": [{"number": i, "id": "<absolute path of the replay test file>::test[i]", ...}]}; it holds local
        paths, so it is generated, not committed)
"""
import json, os, pickle, subprocess, sys, zlib
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
main_map, unit_dir, REAL, prefix, out = sys.argv[1:6]
tmp = out + ".raw"
env = dict(os.environ, MAP_ISO=os.path.join(unit_dir, "iso"), MAP_FN=os.path.join(unit_dir, "fn"))
subprocess.run([sys.executable, os.path.join(HERE, "mkmap.py"), os.path.join(unit_dir, "tests.json"), tmp], env=env, check=True)
q = pickle.loads(zlib.decompress(open(tmp, "rb").read())); os.remove(tmp)
m = pickle.loads(zlib.decompress(open(main_map, "rb").read()))
real = m["ids"].index(REAL); n = len(q["ids"]); hbit = 1 << n
mb = lambda i: int.from_bytes(m["sets"][i], "little"); qb = lambda i: int.from_bytes(q["sets"][i], "little")
width = (n + 1 + 7) // 8
sets, idx = [], {}
def sid(b):
    if b not in idx:
        idx[b] = len(sets); sets.append(b.to_bytes(width, "little"))
    return idx[b]
# UMAP_PHASE=<dir of <unit>.json> with UMAP_SRC=<pinned checkout>: keep, for each unit, only the lines that run in
# its second phase (for a TPC-DS query: inside PythonExecutor.execute) plus the lines it runs that are import-time
# code (module and class bodies, def headers), whose effects any later phase can see. The map then answers "can this
# patch change what the unit's second phase does, given the same inputs?".
phase_dir, src_dir = os.environ.get("UMAP_PHASE"), os.environ.get("UMAP_SRC")
phase = [json.load(open(os.path.join(phase_dir, f"{i}.json"))) for i in range(n)] if phase_dir else None
def import_level(f):
    import ast
    text = open(os.path.join(src_dir, f), encoding="utf-8").read(); tree = ast.parse(text)
    keep = set(range(1, text.count("\n") + 2))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            keep -= set(range(node.body[0].lineno if not getattr(node.body[0], "decorator_list", None) else min(d.lineno for d in node.body[0].decorator_list), node.end_lineno + 1))
    return keep
files = {}; added = 0
for f in set(m["files"]) | set(q["files"]):
    lines = {}
    if f in q["files"]:
        for l, i in zip(*q["files"][f]): lines[l] = qb(i)
    harness_lines = set()
    if f in m["files"]:
        for l, i in zip(*m["files"][f]):
            if mb(i) >> real & 1 and not lines.get(l): lines[l] = hbit; added += 1; harness_lines.add(l)
    if phase is not None and f.endswith(".py"):
        imp = import_level(f); second = [set(p.get(f, ())) for p in phase]
        for l in list(lines):
            if l in harness_lines or l in imp: continue
            b = 0
            for u in range(n):
                if lines[l] >> u & 1 and l in second[u]: b |= 1 << u
            if b: lines[l] = b
            else: del lines[l]
    if lines:
        ks = sorted(lines); files[f] = (ks, [sid(lines[k]) for k in ks])
reads = {k: qb(i) for k, i in q["reads"].items()}
for k, i in m["reads"].items():
    if mb(i) >> real & 1 and not reads.get(k): reads[k] = hbit
fn = {f: {l: qb(i) for l, i in d.items()} for f, d in q["fn"].items()}
for f, d in m["fn"].items():
    for l, i in d.items():
        if mb(i) >> real & 1 and not fn.get(f, {}).get(l): fn.setdefault(f, {})[l] = hbit
inst = {c: qb(i) for c, i in q["inst"].items()}
for c, i in m["inst"].items():
    if mb(i) >> real & 1 and not inst.get(c): inst[c] = hbit
blob = zlib.compress(pickle.dumps({"ids": [f"{prefix}{i}" for i in range(n)] + ["harness"], "sets": sets, "files": files, "reads": {k: sid(v) for k, v in reads.items()},
                                   "subproc": 0, "fn": {f: {l: sid(b) for l, b in d.items()} for f, d in fn.items()},
                                   "inst": {c: sid(b) for c, b in inst.items()}, "fn_missing": q.get("fn_missing", 0), "real": REAL}, protocol=5), 9)
open(out, "wb").write(blob)
print("units", n, "lines only the real test reaches", added, "bytes", len(blob), file=sys.stderr)
