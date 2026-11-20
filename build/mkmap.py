"""Aggregate build/iso/*.json into map.bin (zlib pickle): line -> test bitset, file reads, subprocess tests."""
import json, os, pickle, sys, zlib
HERE = os.path.dirname(os.path.abspath(__file__))
tests = json.load(open(sys.argv[1]))["tests"]
out = sys.argv[2]
lines = {}   # file -> {line: bitset}
reads = {}   # path -> bitset
subproc = 0
missing = []
for t in tests:
    n = t["number"]; p = os.path.join(HERE, "iso", f"{n}.json")
    if not os.path.exists(p):
        missing.append(t["id"]); continue
    d = json.load(open(p)); bit = 1 << n
    for part in ("pre", "run"):
        for f, ls in d[part].items():
            m = lines.setdefault(f, {})
            for l in ls:
                m[l] = m.get(l, 0) | bit
    for r in d["reads"]:
        reads[r] = reads.get(r, 0) | bit
    if d["subproc"]:
        subproc |= bit
fn, inst, fn_missing = {}, {}, 0   # file -> {firstlineno: bitset}; "module:Class" -> bitset
for t in tests:
    p = os.path.join(HERE, "fn", f"{t['number']}.json")
    if not os.path.exists(p):
        fn_missing |= 1 << t["number"]; continue
    d = json.load(open(p)); bit = 1 << t["number"]
    for f, ls in d["fn"].items():
        m = fn.setdefault(f, {})
        for l in ls: m[l] = m.get(l, 0) | bit
    for c in d["inst"]: inst[c] = inst.get(c, 0) | bit
sets, idx, files = [], {}, {}
def sid(b):
    if b not in idx:
        idx[b] = len(sets); sets.append(b.to_bytes((len(tests) + 7) // 8, "little"))
    return idx[b]
for f, m in lines.items():
    ks = sorted(m)
    files[f] = (ks, [sid(m[k]) for k in ks])
blob = zlib.compress(pickle.dumps({"ids": [t["id"] for t in tests], "sets": sets, "files": files,
                                   "reads": {k: sid(v) for k, v in reads.items()}, "subproc": subproc,
                                   "fn": {f: {l: sid(b) for l, b in m.items()} for f, m in fn.items()},
                                   "inst": {c: sid(b) for c, b in inst.items()}, "fn_missing": fn_missing}, protocol=5), 9)
open(out, "wb").write(blob)
print("files", len(files), "lines", sum(len(v[0]) for v in files.values()), "unique sets", len(sets), "bytes", len(blob), "fn files", len(fn), "inst classes", len(inst), "fn_missing", bin(fn_missing).count("1"), "missing", len(missing), missing[:5], file=sys.stderr)
