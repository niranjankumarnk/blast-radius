"""Blast Radius selector: map changed lines to the tests that execute them (per-test coverage of the pinned commit)."""
import ast, bisect, json, os, pickle, re, sys, zlib
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/opt/sqlglot"))
MAP = Path(os.environ.get("BR_MAP", Path(__file__).with_name("map.bin")))
WORKER_MS, MAX_WORKERS = 2000, 8
ALL_TRIGGERS = ("conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "setup.py")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    """-> list of (old_path|None, new_path|None, hunks); hunk = (old_start, [(tag, line), ...])."""
    files, cur, lines, i = [], None, text.split("\n"), 0
    while i < len(lines):
        l = lines[i]
        if l.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", l)
            cur = [m.group(1), m.group(2), []] if m else [None, None, []]
            files.append(cur)
        elif cur is not None and l.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            a, b = l[4:].split("\t")[0], lines[i + 1][4:].split("\t")[0]
            cur[0] = None if a == "/dev/null" else a[2:] if a[:2] in ("a/", "b/") else a
            cur[1] = None if b == "/dev/null" else b[2:] if b[:2] in ("a/", "b/") else b
            i += 1
        elif cur is not None and l.startswith(("deleted file mode", "rename from", "rename to", "new file mode", "old mode", "new mode", "GIT binary", "Binary files")):
            cur.append(l.split(" ")[0])
        elif cur is not None and (m := HUNK.match(l)):
            n_old = int(m.group(2) or 1); n_new = int(m.group(4) or 1)
            body = []; i += 1
            while i < len(lines) and (n_old > 0 or n_new > 0):
                h = lines[i]; tag = h[:1]
                if tag == "\\":
                    i += 1; continue
                if tag == "-": n_old -= 1
                elif tag == "+": n_new -= 1
                else: n_old -= 1; n_new -= 1; tag = " "
                body.append((tag, h[1:])); i += 1
            cur[2].append((int(m.group(1)), body))
            continue
        i += 1
    return files


def apply_hunks(old_lines, hunks):
    """-> (new_lines, removed old line numbers (1-based), insertion anchors as old line numbers)."""
    out, pos, removed, anchors = [], 0, set(), set()
    for start, body in hunks:
        s = max(start - 1, 0) if any(t != "+" for t, _ in body) else start
        out.extend(old_lines[pos:s]); pos = s
        for tag, text in body:
            if tag == " ":
                out.append(old_lines[pos] if pos < len(old_lines) else text); pos += 1
            elif tag == "-":
                removed.add(pos + 1); pos += 1
            else:
                out.append(text); anchors.add(pos)        # between old line pos and pos+1
    out.extend(old_lines[pos:])
    return out, removed, anchors


class Index:
    """Statement spans of the pinned source file."""
    def __init__(self, tree):
        self.stmts = []     # (lo, hi, header_hi, node, func, cls)   func = innermost enclosing def or None; cls = "A.B" or None
        self._walk(tree.body, None, None)
        self.stmts.sort(key=lambda s: (s[0], -s[1]))

    def _walk(self, body, func, cls):
        for n in body:
            lo = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
            hi = n.end_lineno
            inner = [b for f in ("body", "orelse", "finalbody", "handlers", "cases") for b in getattr(n, f, []) or []]
            hdr = min(b.lineno for b in inner) - 1 if inner else hi
            self.stmts.append((lo, hi, max(hdr, lo), n, func, cls))
            f2 = n if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            c2 = ((cls + "." if cls else "") + n.name) if isinstance(n, ast.ClassDef) else None if f2 is not func else cls
            for f in ("body", "orelse", "finalbody"):
                self._walk(getattr(n, f, []) or [], f2, c2)
            for h in getattr(n, "handlers", []) or []:
                self.stmts.append((h.lineno, h.end_lineno, h.body[0].lineno - 1 if h.body else h.end_lineno, h, func, cls))
                self._walk(h.body, f2, c2)
            for c in getattr(n, "cases", []) or []:
                self._walk(c.body, f2, c2)

    def innermost(self, line):
        best = None
        for s in self.stmts:
            if s[0] > line: break
            if s[1] >= line: best = s
        return best


FUNC = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_STUB = ast.Constant(value="<body>")


def func_nodes(tree):
    """Function-like nodes in a deterministic order (parents before children)."""
    out, stack = [], [tree]
    while stack:
        n = stack.pop()
        if isinstance(n, FUNC): out.append(n)
        stack.extend(reversed(list(ast.iter_child_nodes(n))))
    return out


def strip_bodies(tree):
    """Dump of the tree with every function-like body replaced, and the own-body dump of each function-like."""
    fns = func_nodes(tree); saved = [f.body for f in fns]
    for f in fns: f.body = _STUB if isinstance(f, ast.Lambda) else [ast.Expr(value=_STUB)]
    skeleton = ast.dump(tree)
    own = []
    for f, b in zip(reversed(fns), reversed(saved)):      # children first: restore, dump with nested bodies stubbed
        f.body = b
    for f in fns:
        inner = [g for g in func_nodes(f) if g is not f]; keep = [g.body for g in inner]
        for g in inner: g.body = _STUB if isinstance(g, ast.Lambda) else [ast.Expr(value=_STUB)]
        own.append(ast.dump(f.body) if isinstance(f, ast.Lambda) else "".join(ast.dump(x) for x in f.body))
        for g, b in zip(inner, keep): g.body = b
    return skeleton, fns, own


CORE_STEMS = {"dialect", "__init__"}
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")


class Selector:
    def __init__(self, tests):
        m = pickle.loads(zlib.decompress(MAP.read_bytes()))
        self.sets, self.files, self.reads, self.subproc = m["sets"], m["files"], m["reads"], m["subproc"]
        self.fn, self.inst, self.fn_missing = m.get("fn", {}), m.get("inst", {}), m.get("fn_missing", 0)
        self._src = None
        num = {t["id"]: t["number"] for t in tests}
        self.remap = None if [num.get(i) for i in m["ids"]] == list(range(len(m["ids"]))) and len(num) == len(m["ids"]) else [num.get(i) for i in m["ids"]]
        self.unknown = [t["number"] for t in tests if t["id"] not in set(m["ids"])] if self.remap else []
        self.all = (1 << len(m["ids"])) - 1
        self.by_file = {}
        for i, tid in enumerate(m["ids"]):
            self.by_file[tid.split("::")[0]] = self.by_file.get(tid.split("::")[0], 0) | (1 << i)

    def bits(self, idx): return int.from_bytes(self.sets[idx], "little")

    def cover(self, path, lo, hi):
        f = self.files.get(path)
        if not f: return 0
        ks, ix = f; r = 0
        for j in range(bisect.bisect_left(ks, lo), bisect.bisect_right(ks, hi)):
            r |= self.bits(ix[j])
        return r

    def fn_tests(self, path, line):
        i = self.fn.get(path, {}).get(line)
        return (self.bits(i) if i is not None else 0) | self.fn_missing

    def name_users(self, name, skip):
        """Tests that execute any line mentioning `name` (outside `skip` = {(path, line)}); None if nothing mentions it."""
        if self._src is None:
            self._src = {str(p.relative_to(SRC)): p.read_text(encoding="utf-8") for p in sorted((SRC / "sqlglot").rglob("*.py"))}
        pat = re.compile(r"\b" + re.escape(name) + r"\b"); r = 0; found = False
        define = re.compile(r"\s*" + re.escape(name) + r"\s*(:|=(?!=))")      # a (re)definition in some class body reads nothing
        for path, text in self._src.items():
            if name not in text: continue
            for i, line in enumerate(text.split("\n"), 1):
                if (path, i) in skip or not pat.search(line) or line.lstrip().startswith("#"): continue
                if define.match(line) and len(pat.findall(line)) == 1: continue
                found = True; r |= self.cover(path, i, i)
        return r if found else None

    def class_attr(self, path, node, cls, whole):
        """A changed assignment in a class body: only tests that build an instance of the class's family can see it."""
        parts = path[:-3].split("/"); mod = ".".join(parts); r = None
        if len(parts) == 3 and parts[1] == "expressions":
            i = self.inst.get(mod + ":" + cls)
            r = self.bits(i) if i is not None else None
        elif len(parts) == 3 and parts[1] in ("dialects", "parsers", "generators") and parts[2] not in CORE_STEMS:
            mods = {f"sqlglot.{d}.{parts[2]}" for d in ("dialects", "parsers", "generators")}; g = 0
            for k, i in self.inst.items():
                if k.split(":")[0] in mods: g |= self.bits(i)
            r = g or None
            if r is not None:
                names = [t.id for t in (node.targets if isinstance(node, ast.Assign) else [node.target]) if isinstance(t, ast.Name)]
                if names and all(NAME_RE.match(n) for n in names):
                    skip = {(path, l) for l in range(node.lineno, node.end_lineno + 1)}; u = 0
                    for n in names:
                        x = self.name_users(n, skip)
                        if x is None: u = None; break
                        u |= x
                    if u is not None: r &= u
        if r is None: return whole
        return (r | self.fn_missing) & self.all

    def file_tests(self, path):
        f = self.files.get(path); r = 0
        if f:
            for j in set(f[1]): r |= self.bits(j)
        return r

    def readers(self, path, parents=False):
        r = self.bits(self.reads[path]) if path in self.reads else 0
        d = os.path.dirname(path) if parents else ""
        while d:                       # tests that list a parent directory see new/removed/renamed files
            if d in self.reads: r |= self.bits(self.reads[d])
            d = os.path.dirname(d)
        return r

    def select(self, patch_text):
        sel = 0
        for f in parse_patch(patch_text):
            old, new, hunks, flags = f[0], f[1], f[2], f[3:]
            for p in {old, new} - {None}:
                if os.path.basename(p) in ALL_TRIGGERS: return self.all
            if old is None or new is None or old != new or flags and not hunks:      # add / delete / rename / mode / binary
                for p in {old, new} - {None}:
                    sel |= self.readers(p, True) | self.file_tests(p)
                    if p.endswith(".py") and p.startswith("sqlglot/"): sel |= self.subproc
                    if p.endswith("__init__.py") or (p.endswith(".py") and old != new and old and new): return self.all
                if old is None or new is None: continue
            path = old
            if not path.endswith(".py"):
                sel |= self.readers(path); continue
            sel |= self.py_change(path, hunks) | self.readers(path)
        return sel

    def py_change(self, path, hunks):
        try:
            old_text = (SRC / path).read_text(encoding="utf-8")
        except OSError:
            return self.all
        old_lines = old_text.split("\n")
        new_lines, removed, anchors = apply_hunks(old_lines, hunks)
        try:
            old_tree = ast.parse(old_text)
        except SyntaxError:
            return self.all
        whole = self.file_tests(path) | (self.subproc if path.startswith("sqlglot/") else 0)
        if path.startswith("tests/") and path in self.by_file: whole |= self.by_file[path]
        try:
            new_tree = ast.parse("\n".join(new_lines))
        except (SyntaxError, ValueError):
            return whole
        if ast.dump(old_tree) == ast.dump(new_tree):
            return 0
        idx = Index(old_tree); r = 0
        n = len(old_lines)
        sk_old, fns_old, own_old = strip_bodies(old_tree)
        sk_new, fns_new, own_new = strip_bodies(new_tree)
        bodies_only = sk_old == sk_new and len(fns_old) == len(fns_new)      # every change is inside a function-like body
        if bodies_only:
            for f, a, b in zip(fns_old, own_old, own_new):
                if a != b and isinstance(f, ast.Lambda):
                    r |= self.fn_tests(path, f.lineno)
        def code(i): return 1 <= i <= n and old_lines[i - 1].strip() and not old_lines[i - 1].strip().startswith("#")
        targets = set(removed)
        for a in anchors:                      # inserted lines: nearest code line on each side
            lo = a
            while lo >= 1 and not code(lo): lo -= 1
            hi = a + 1
            while hi <= n and not code(hi): hi += 1
            if lo >= 1: targets.add(lo)
            if hi <= n: targets.add(hi)
        for line in targets:
            s = idx.innermost(line)
            if s is None:
                r |= whole; continue          # module level outside any statement
            lo, hi, hdr, node, func, cls = s
            if bodies_only and func is None and not isinstance(node, FUNC):
                continue                       # import-time statement whose only changes are lambda bodies (handled above)
            if not bodies_only and func is None and cls and isinstance(node, (ast.Assign, ast.AnnAssign)):
                r |= self.class_attr(path, node, cls, whole); continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and line <= hdr and not node.decorator_list:
                c = self.cover(path, hdr + 1, hi)          # signature change: whoever runs the body
            elif isinstance(node, ast.ClassDef) or func is None:
                c = self.cover(path, lo, hdr) or whole     # import-time code
            else:
                c = self.cover(path, lo, hdr)
            r |= c
        if path.startswith("sqlglot/") and r: r |= self.subproc
        return r


def pack(numbers, ms):
    if not numbers: return []
    order = sorted(numbers, key=lambda n: -ms[n]); best = None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        loads, ws = [0] * k, [[] for _ in range(k)]
        for n in order:
            j = loads.index(min(loads)); ws[j].append(n); loads[j] += ms[n]
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]: best = (cost, ws)
    return best[1]


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    s = Selector(tests); out = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            b = s.select(patch.read_text(encoding="utf-8", errors="surrogateescape"))
        except Exception as e:                                   # never miss because of a bug: run everything
            print(f"{patch.stem}: {type(e).__name__}: {e}", file=sys.stderr); b = s.all
        nums = [i for i in range(b.bit_length()) if b >> i & 1]
        if s.remap: nums = [s.remap[i] for i in nums if s.remap[i] is not None] + (s.unknown if nums else [])
        print(f"{patch.stem}: {len(nums)} tests", file=sys.stderr)
        out.append(json.dumps({"commit": patch.stem, "workers": pack(nums, ms)}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
