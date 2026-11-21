"""Blast Radius selector: map changed lines to the tests that execute them (per-test coverage of the pinned commit)."""
import ast, bisect, difflib, json, os, pickle, re, shutil, signal, sys, time, zlib
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/opt/sqlglot"))
MAP = Path(os.environ.get("BR_MAP", Path(__file__).with_name("map.bin")))
WORKER_MS, MAX_WORKERS = 2000, 8
ALL_TRIGGERS = ("conftest.py", "pytest.ini", ".pytest.ini", "sitecustomize.py", "usercustomize.py")
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
        elif l.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if cur is None or cur[2] or cur.count("---"):          # a plain unified diff, with no "diff --git" line
                cur = [None, None, []]; files.append(cur)
            cur.append("---")
            a, b = l[4:].split("\t")[0].strip().strip('"'), lines[i + 1][4:].split("\t")[0].strip().strip('"')
            cur[0] = None if a == "/dev/null" else a[2:] if a[:2] in ("a/", "b/") else a
            cur[1] = None if b == "/dev/null" else b[2:] if b[:2] in ("a/", "b/") else b
            i += 1
        elif cur is not None and l.startswith(("deleted file mode", "rename from", "rename to", "new file mode", "old mode", "new mode", "GIT binary", "Binary files")):
            cur.append(" ".join(l.split(" ")[:2]))
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
            hdr = min(b.lineno if hasattr(b, "lineno") else b.pattern.lineno for b in inner) - 1 if inner else hi
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


def strip_docstrings(tree):
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
            b = n.body[0]
            if isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant) and isinstance(b.value.value, str): b.value.value = ""
    return ast.dump(tree)


def bound_defs(tree):
    """Qualified name -> node, for the functions and classes defined at module and class level."""
    out = {}
    def walk(body, prefix):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out[prefix + n.name] = n
                if isinstance(n, ast.ClassDef): walk(n.body, prefix + n.name + ".")
            for f in ("body", "orelse", "finalbody"):
                if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): walk(getattr(n, f, []) or [], prefix)
    walk(tree.body, "")
    return out


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


# ---- statement-level comparison of the pinned file and the patched file ---------------------------------------------
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
TRY = (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())
MATCH = (ast.Match,) if hasattr(ast, "Match") else ()
BRANCHY = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith) + MATCH
COMPOUND = BRANCHY + TRY + DEFS + (ast.ClassDef,)
BLOCK_FIELDS = ("body", "orelse", "finalbody")
COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


def stmt_lo(n):
    return min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])


def stmt_hdr(n):
    """Last line of a statement's own header: the line before its first nested statement."""
    firsts = []
    for f in BLOCK_FIELDS:
        b = getattr(n, f, None)
        if isinstance(b, list) and b and isinstance(b[0], ast.stmt): firsts.append(stmt_lo(b[0]))
    firsts += [h.lineno for h in getattr(n, "handlers", []) or []]
    firsts += [c.pattern.lineno for c in getattr(n, "cases", []) or []]
    return max(min(firsts) - 1, stmt_lo(n)) if firsts else n.end_lineno


def own_dump(n, name=True):
    """A compound statement without its nested blocks (and, for name=False, without its own name)."""
    saved = {}
    for f in BLOCK_FIELDS + ("handlers", "cases"):
        v = getattr(n, f, None)
        if isinstance(v, list): saved[f] = v; setattr(n, f, [])
    keep = getattr(n, "name", None)
    if not name and isinstance(keep, str): n.name = ""
    try:
        return ast.dump(n)
    finally:
        for f, v in saved.items(): setattr(n, f, v)
        if not name and isinstance(keep, str): n.name = keep


def noexec(n, imp=False):
    """Statements that execute nothing, so line coverage never shows them."""
    return isinstance(n, (ast.Pass, ast.Global, ast.Nonlocal)) or (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)) \
        or (not imp and isinstance(n, ast.AnnAssign) and n.value is None)      # (at import time it fills __annotations__)


def trivial_def(f, lazy_annotations):
    """A `def` statement that only binds a name when it runs: no decorators, constant defaults, unevaluated annotations."""
    g = f.args
    if f.decorator_list or getattr(f, "type_params", None): return False
    if not all(isinstance(d, ast.Constant) for d in g.defaults + [d for d in g.kw_defaults if d is not None]): return False
    return lazy_annotations or (f.returns is None and all(x.annotation is None for x in g.posonlyargs + g.args + g.kwonlyargs + [v for v in (g.vararg, g.kwarg) if v]))


def lambda_parts(stmt):
    """(the statement with every lambda body stubbed, its lambdas, each lambda's own body with nested lambdas stubbed)."""
    lams = [n for n in func_nodes(stmt) if isinstance(n, ast.Lambda)]
    saved = [f.body for f in lams]
    for f in lams: f.body = _STUB
    skel = ast.dump(stmt)
    for f, b in zip(lams, saved): f.body = b
    own = []
    for f in lams:
        inner = [g for g in func_nodes(f.body) if isinstance(g, ast.Lambda)]; keep = [g.body for g in inner]
        for g in inner: g.body = _STUB
        own.append(ast.dump(f.body))
        for g, b in zip(inner, keep): g.body = b
    return skel, lams, own


def local_bindings(fn):
    """The names a function binds in its own scope, and its global / nonlocal declarations. A change to either set
    changes what every other use of the name in the function means."""
    names, decl = set(), set()
    a = fn.args
    for x in a.posonlyargs + a.args + a.kwonlyargs + ([a.vararg] if a.vararg else []) + ([a.kwarg] if a.kwarg else []): names.add(x.arg)
    stack = list(fn.body)
    while stack:
        n = stack.pop()
        if isinstance(n, DEFS + (ast.ClassDef,)):
            names.add(n.name); stack.extend(n.decorator_list); continue           # (its body is another scope)
        if isinstance(n, ast.Lambda):
            continue
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for sub in ast.walk(n):                                               # only := escapes a comprehension
                if isinstance(sub, ast.NamedExpr) and isinstance(sub.target, ast.Name): names.add(sub.target.id)
            continue
        if isinstance(n, ast.Name):
            if isinstance(n.ctx, (ast.Store, ast.Del)): names.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for al in n.names: names.add((al.asname or al.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler):
            if n.name: names.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            decl.update((type(n).__name__, x) for x in n.names)
        elif MATCH and isinstance(n, (ast.MatchAs, ast.MatchStar)):
            if n.name: names.add(n.name)
        elif MATCH and isinstance(n, ast.MatchMapping):
            if n.rest: names.add(n.rest)
        stack.extend(ast.iter_child_nodes(n))
    return names, decl


def future_annotations(tree):
    return any(isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names) for n in tree.body)


def def_time_same(a, b, lazy_annotations):
    """Do two definitions of one function do the same work when the `def` statement itself runs? True when only
    parameter names, constant defaults and (unevaluated) annotations differ: then only callers can tell."""
    if [ast.dump(d) for d in a.decorator_list] != [ast.dump(d) for d in b.decorator_list]: return False
    if [ast.dump(x) for x in getattr(a, "type_params", [])] != [ast.dump(x) for x in getattr(b, "type_params", [])]: return False
    da = a.args.defaults + [d for d in a.args.kw_defaults if d is not None]; db = b.args.defaults + [d for d in b.args.kw_defaults if d is not None]
    if len(da) != len(db): return all(isinstance(d, ast.Constant) for d in da + db)
    for x, y in zip(da, db):
        if ast.dump(x) != ast.dump(y) and not (isinstance(x, ast.Constant) and isinstance(y, ast.Constant)): return False
    if not lazy_annotations:
        def notes(f):
            g = f.args
            return [ast.dump(x.annotation) if x.annotation is not None else "" for x in g.posonlyargs + g.args + g.kwonlyargs + [v for v in (g.vararg, g.kwarg) if v]] + [ast.dump(f.returns) if f.returns is not None else ""]
        if notes(a) != notes(b): return False
    return True


class Ctx:
    """Where a block runs: imp = at import time (not inside any def); cls = qualified name when directly in a class body."""
    __slots__ = ("imp", "cls")
    def __init__(self, imp, cls): self.imp, self.cls = imp, cls


CORE_STEMS = {"dialect", "__init__"}
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")


class Selector:
    def __init__(self, tests, map_path=None):
        m = pickle.loads(zlib.decompress(Path(map_path or MAP).read_bytes()))
        self.meta = m
        self.sets, self.files, self.reads, self.subproc = m["sets"], m["files"], m["reads"], m["subproc"]
        self.fn, self.inst, self.fn_missing = m.get("fn", {}), m.get("inst", {}), m.get("fn_missing", 0)
        self._src = None; self._tsrc = None; self._docs = None; self._res = None; self._idx = {}; self._tokens = None
        num = {t["id"]: t["number"] for t in tests}
        self.remap = None if [num.get(i) for i in m["ids"]] == list(range(len(m["ids"]))) and len(num) == len(m["ids"]) else [num.get(i) for i in m["ids"]]
        self.unknown = [t["number"] for t in tests if t["id"] not in set(m["ids"])] if self.remap else []
        self.all = (1 << len(m["ids"])) - 1
        self.by_file = {}
        for i, tid in enumerate(m["ids"]):
            self.by_file[tid.split("::")[0]] = self.by_file.get(tid.split("::")[0], 0) | (1 << i)
        self.test_dirs = set()
        for f in self.by_file:
            d = os.path.dirname(f)
            while d and d not in self.test_dirs: self.test_dirs.add(d); d = os.path.dirname(d)

    def bits(self, idx): return int.from_bytes(self.sets[idx], "little")

    def note(self, x):
        """Remember each changed spot's own test set (used to decide whether a heavy test has cheap witnesses)."""
        if x: self.parts.append(x)
        return x

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

    def sources(self, tests=False):
        if self._src is None:
            self._src = {str(p.relative_to(SRC)): p.read_text(encoding="utf-8") for p in sorted((SRC / "sqlglot").rglob("*.py"))}
        if tests and self._tsrc is None:
            self._tsrc = {}
            for p in sorted((SRC / "tests").rglob("*.py")):
                try: self._tsrc[str(p.relative_to(SRC))] = p.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError): pass
        return {**self._src, **self._tsrc} if tests else self._src

    def tree_of(self, p):
        if p not in self._idx:
            try: self._idx[p] = ast.parse(self.sources(True)[p])
            except (SyntaxError, KeyError, ValueError): self._idx[p] = None
        return self._idx[p]

    def import_time_mentions(self, p, name):
        """Lines of file p where `name` is referred to by code that runs when the file is imported: class and module
        bodies, decorators, default values, but not the inside of a function or lambda."""
        tree = self.tree_of(p)
        if tree is None: return None
        out = set()
        def visit(n, deferred):
            if not deferred and ((isinstance(n, ast.Attribute) and n.attr == name) or (isinstance(n, ast.Name) and n.id == name)):
                out.add(n.lineno)
            if isinstance(n, DEFS + (ast.Lambda,)):
                for c in ast.iter_child_nodes(n):
                    body = n.body if isinstance(n.body, list) else [n.body]
                    visit(c, deferred or any(c is x for x in body))
            else:
                for c in ast.iter_child_nodes(n): visit(c, deferred)
        visit(tree, False)
        return out

    def gone_name(self, qual, path, idx=None):
        """A function or class name that no longer exists (or a new one). Callers are found through the body's own
        coverage; this adds the references that never run the body. A module-level name: every executed line that
        mentions it, in any file (imports by name included). A method: only references that are evaluated at import
        time (class bodies, decorators, defaults), because an attribute lookup made from inside a function that
        reached this definition is already in the body's coverage."""
        name = qual.split(".")[-1]; method = "." in qual
        pat = re.compile(r"\b" + re.escape(name) + r"\b"); r = 0
        for p, text in self.sources(True).items():
            if name not in text or not pat.search(text): continue
            lines = self.import_time_mentions(p, name) if method else None
            if lines is None: lines = [i for i, line in enumerate(text.split("\n"), 1) if pat.search(line)]
            for i in lines: r |= self.cover(p, i, i)
        return r

    def name_users(self, name, skip):
        """Tests that execute any line mentioning `name` (outside `skip` = {(path, line)}); None if nothing mentions it."""
        pat = re.compile(r"\b" + re.escape(name) + r"\b"); r = 0; found = False
        define = re.compile(r"\s*" + re.escape(name) + r"\s*(:|=(?!=))")      # a (re)definition in some class body reads nothing
        for path, text in self.sources(True).items():
            if name not in text: continue
            for i, line in enumerate(text.split("\n"), 1):
                if (path, i) in skip or not pat.search(line) or line.lstrip().startswith("#"): continue
                if define.match(line) and len(pat.findall(line)) == 1: continue
                found = found or not path.startswith("tests/")      # a name nothing in sqlglot/ mentions is read some other way
                r |= self.cover(path, i, i)
        return r if found else None

    def class_users(self, path, cls):
        """Tests that build an instance of a class, or of its dialect family; None when that is not tracked."""
        parts = path[:-3].split("/"); mod = ".".join(parts)
        if len(parts) == 3 and parts[1] == "expressions":
            i = self.inst.get(mod + ":" + cls)
            return self.bits(i) if i is not None else None
        if len(parts) == 3 and parts[1] in ("dialects", "parsers", "generators") and parts[2] not in CORE_STEMS:
            mods = {f"sqlglot.{d}.{parts[2]}" for d in ("dialects", "parsers", "generators")}; g = 0
            for k, i in self.inst.items():
                if k.split(":")[0] in mods: g |= self.bits(i)
            return g or None
        return None

    def class_attr(self, path, node, cls, whole, new=False):
        """A changed assignment in a class body: only tests that build an instance of the class's family can see it."""
        r = self.class_users(path, cls)
        if r is not None and path.split("/")[1] != "expressions":
            names = [t.id for t in (node.targets if isinstance(node, ast.Assign) else [node.target]) if isinstance(t, ast.Name)]
            if names and all(NAME_RE.match(n) for n in names):
                skip = set() if new else {(path, l) for l in range(node.lineno, node.end_lineno + 1)}; u = 0
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
            if d in self.reads and d not in self.test_dirs: r |= self.bits(self.reads[d])
            d = os.path.dirname(d)             # (pytest itself lists the directories that hold test files, for every test)
        return r

    def select(self, patch_text):
        self.parts = []
        r = self._select(patch_text)
        if r == self.all: self.parts = [r]
        return r

    def touches_pytest(self, path, hunks):
        """Does an edit to a root config file change what pytest reads from it?"""
        try:
            lines = (SRC / path).read_text(encoding="utf-8").split("\n")
            if not hunks_fit(lines, hunks): return True
            new = apply_hunks(lines, hunks)[0]
        except Exception:
            return True
        sect = re.compile(r"^\s*\[(tool[.:]pytest|pytest|tool\.coverage|coverage)", re.M)
        if sect.search("\n".join(lines)) or sect.search("\n".join(new)): return True
        return any("pytest" in text.lower() or text.lstrip().startswith("[") for _, body in hunks for tag, text in body if tag != " ")

    def resolvers(self):
        """Test files that resolve symlinks: they would look past the patched overlay, so their tests are never run here."""
        if self._res is None:
            self._res = {p for p, text in self.sources(True).items() if p.startswith("tests/") and any(k in text for k in ("resolve(", "realpath", "readlink", "samefile"))}
        return self._res

    def reads_docstrings(self):
        if self._docs is None:
            self._docs = any(k in text for p, text in self.sources(True).items() if p in self.by_file for k in ("__doc__", "doctest", "getdoc"))
        return self._docs

    def untraced(self, path):
        """A file the coverage run could not see (outside sqlglot/ and tests/): the tests of every test file that
        names it, or one of its directories, as a path component in a string (URLs do not count)."""
        if self._tokens is None:
            self._tokens = {}
            for tf in self.sources(True):
                if not tf.startswith("tests/"): continue
                tree = self.tree_of(tf); toks = set()
                for n in ast.walk(tree) if tree is not None else ():
                    if isinstance(n, ast.Constant) and isinstance(n.value, str) and "://" not in n.value and len(n.value) < 300:
                        toks.update(x for x in re.split(r"[\\/]", n.value) if x)
                self._tokens[tf] = toks
        keys = {os.path.basename(path)} | {d for d in path.split("/")[:-1] if d not in ("sqlglot", "tests", "fixtures", "")}
        r = 0
        for tf, toks in self._tokens.items():
            if keys & toks: r |= self.by_file.get(tf, 0) | self.file_tests(tf)
        return r

    def _select(self, patch_text):
        sel = 0
        files = parse_patch(patch_text)
        if not files and patch_text.strip(): return self.all            # not a diff we understand
        for f in files:
            old, new, hunks, flags = f[0], f[1], f[2], [x for x in f[3:] if x != "---"]
            if "new file" in flags: old = None                 # (an empty file has no ---/+++ lines to say so)
            if "deleted file" in flags: new = None
            if old is None and new is None:
                if hunks or flags: return self.all
                continue
            if old == new and not hunks and set(flags) <= {"old mode", "new mode"}: continue      # mode change only
            for p in {old, new} - {None}:
                top = p.split("/")[0]
                if "/" not in p and p.endswith((".pth", ".cfg", ".ini", ".toml")) and (
                        old is None or new is None or old != new or not hunks or self.touches_pytest(p, hunks)): return self.all
                if "/" not in p and p.endswith(".py") and (old is None or new is None or old != new): return self.all   # a root-level module can shadow any import
                if top not in ("sqlglot", "tests") and p not in self.files and p not in self.reads:
                    sel |= self.note(self.untraced(p))
            for p in {old, new} - {None}:
                if os.path.basename(p) in ALL_TRIGGERS: return self.all
            if old is None or new is None or old != new or flags and not hunks:      # add / delete / rename / mode / binary
                for p in {old, new} - {None}:
                    sel |= self.note(self.readers(p, True) | self.file_tests(p) | self.by_file.get(p, 0))
                    if SUBPROC and p.endswith(".py") and p.startswith("sqlglot/"): sel |= self.note(self.subproc)
                    if p.endswith("__init__.py"): return self.all
                if old is None or new is None: continue
            path = old
            if not path.endswith(".py"):
                sel |= self.note(self.readers(path)); continue
            sel |= self.py_change(path, hunks) | self.note(self.readers(path))
        return sel

    def structural(self, path, old_tree, new_tree, whole, old_lines, new_lines):
        """Compare the two versions statement by statement -> (tests, whether anything executable differs). A test
        can behave differently only if, on the pinned code, it executes a statement that was changed or removed, or
        passes a point where one was inserted."""
        lazy = future_annotations(old_tree) and future_annotations(new_tree)
        docs = self.reads_docstrings()
        cover = self.cover
        state = {"differs": False}

        def cov(st):                       # tests that execute an old statement (its own header, for a compound one)
            lo, hi = stmt_lo(st), st.end_lineno
            if isinstance(st, TRY): return cover(path, lo, hi)          # (a bare `try:` line is not always recorded)
            c = cover(path, lo, stmt_hdr(st))
            return c or (cover(path, lo, hi) if isinstance(st, BRANCHY) else 0)

        def enter(fn):                     # tests that run the body of an old function
            r = cover(path, stmt_lo(fn.body[0]), fn.end_lineno) | self.fn_tests(path, fn.lineno)
            return r | (self.fn_tests(path, stmt_lo(fn)) if stmt_lo(fn) != fn.lineno else 0)

        def entry(owner, ctx):             # tests that enter a block of `owner` when no statement of the block can show it
            if owner is None or isinstance(owner, ast.Module): return whole
            if isinstance(owner, DEFS): return enter(owner)
            if isinstance(owner, ast.ClassDef): return whole if ctx.imp else cov(owner)
            if not isinstance(owner, ast.stmt):                         # an except handler or a match case
                lo = owner.lineno if hasattr(owner, "lineno") else owner.pattern.lineno
                hi = owner.body[-1].end_lineno if owner.body else lo
                return cover(path, lo, hi) or cov(owner._owner)
            return cov(owner)

        def reach(block, i, owner, ctx):   # tests that pass the point just before old block[i] (i == len: the block's end)
            for st in block[i:]:
                if not noexec(st, ctx.imp): return cov(st)              # the next statement that executes anything
            for st in reversed(block[:i]):
                if not noexec(st, ctx.imp): return cov(st)              # block end: whoever started its last statement
            return entry(owner, ctx)

        def new_def_users(st, ctx):        # a function under a name that did not exist: whoever can look the name up
            if ctx.cls:
                u = self.class_users(path, ctx.cls)
                return whole if u is None else (u | self.fn_missing) & self.all
            return self.gone_name(st.name, path) & whole

        def inserted(sts, block, i, owner, ctx):
            if not ctx.imp:
                return reach(block, i, owner, ctx) if any(not noexec(b) for b in sts) else 0
            r = 0
            for b in sts:
                if noexec(b, True): continue
                if isinstance(b, DEFS) and trivial_def(b, lazy): r |= new_def_users(b, ctx)
                elif ctx.cls and isinstance(b, (ast.Assign, ast.AnnAssign)): r |= self.class_attr(path, b, ctx.cls, whole, new=True)
                else: r |= reach(block, i, owner, ctx)
            return r

        def gone(a, block, i, owner, ctx):                 # an old statement with no counterpart
            qual = (ctx.cls + "." if ctx.cls else "") + getattr(a, "name", "")
            if isinstance(a, DEFS):                        # callers, and whoever else refers to the name (they imported this file)
                return enter(a) | ((self.gone_name(qual, path) & (cov(a) or whole)) if ctx.imp else cov(a))
            if isinstance(a, ast.ClassDef):                # whoever can name the class ran its `class` statement
                c = cover(path, stmt_lo(a), a.end_lineno)
                return (c or whole) if ctx.imp else c
            if noexec(a, ctx.imp): return 0
            if ctx.imp:
                if ctx.cls and isinstance(a, (ast.Assign, ast.AnnAssign)): return self.class_attr(path, a, ctx.cls, whole)
                return cov(a) or whole
            return cov(a)

        def changed(a, b, block, i, owner, ctx):           # an old statement that became a different one of the same kind
            if ctx.imp:
                ska, la, oa = lambda_parts(a); skb, lb, ob = lambda_parts(b)
                if ska == skb and len(la) == len(lb):      # only lambda bodies differ: whoever calls those lambdas
                    r = 0
                    for f, x, y in zip(la, oa, ob):
                        if x != y: r |= self.fn_tests(path, f.lineno)
                    return r
                if ctx.cls and isinstance(a, (ast.Assign, ast.AnnAssign)): return self.class_attr(path, a, ctx.cls, whole)
                return cov(a) or whole
            if noexec(a): return 0 if noexec(b) else reach(block, i, owner, ctx)
            return cov(a)

        def pair(a, b, block, i, owner, ctx):              # the same kind of statement at the same place, different text
            if isinstance(a, DEFS):
                if a.name != b.name:
                    state["differs"] = True
                    r = gone(a, block, i, owner, ctx)
                    if ctx.imp:
                        same = own_dump(a, False) == own_dump(b, False)
                        r |= new_def_users(b, ctx) if same or trivial_def(b, lazy) else reach(block, i, owner, ctx)
                    return r
                r = 0
                if own_dump(a) != own_dump(b):             # signature, defaults, decorators or annotations
                    state["differs"] = True
                    r |= enter(a)
                    if not def_time_same(a, b, lazy): r |= (cov(a) or whole) if ctx.imp else cov(a)
                r |= blocks(a.body, b.body, a, Ctx(False, None))
                if local_bindings(a) != local_bindings(b): r |= enter(a)
                return r
            if isinstance(a, ast.ClassDef):
                if a.name != b.name:
                    state["differs"] = True
                    return gone(a, block, i, owner, ctx) | (reach(block, i, owner, ctx) if ctx.imp else 0)
                r = 0
                if own_dump(a) != own_dump(b):
                    state["differs"] = True
                    r = (cov(a) or whole) if ctx.imp else cov(a)
                sub = Ctx(ctx.imp, ((ctx.cls + "." if ctx.cls else "") + a.name) if ctx.imp else None)
                return r | blocks(a.body, b.body, a, sub)
            if isinstance(a, COMPOUND) and own_dump(a) == own_dump(b):
                ha, hb = getattr(a, "handlers", None) or [], getattr(b, "handlers", None) or []
                ca, cb = getattr(a, "cases", None) or [], getattr(b, "cases", None) or []
                if len(ha) == len(hb) and len(ca) == len(cb) and all(own_dump(x) == own_dump(y) for x, y in zip(ha, hb)) \
                        and all(ast.dump(x.pattern) == ast.dump(y.pattern) and ast.dump(x.guard or ast.Pass()) == ast.dump(y.guard or ast.Pass()) for x, y in zip(ca, cb)):
                    r = 0
                    for f in BLOCK_FIELDS:
                        x, y = getattr(a, f, None) or [], getattr(b, f, None) or []
                        if x or y: r |= blocks(x, y, a, ctx)
                    for x, y in list(zip(ha, hb)) + list(zip(ca, cb)):
                        x._owner = a; r |= blocks(x.body, y.body, x, ctx)
                    return r
            if ast.dump(a) == ast.dump(b): return 0        # the same statement, laid out or commented differently
            state["differs"] = True
            return changed(a, b, block, i, owner, ctx)

        def key(st, lines):                # equal source text at the same columns is the same statement
            return (st.col_offset, st.end_col_offset, "\n".join(lines[stmt_lo(st) - 1:st.end_lineno]))

        def is_doc(st):
            return isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)

        def blocks(old, new, owner, ctx):
            if not docs and isinstance(owner, DEFS + (ast.ClassDef, ast.Module)):      # docstrings are not executed
                if old and is_doc(old[0]): old = old[1:]
                if new and is_doc(new[0]): new = new[1:]
            ko = [key(x, old_lines) for x in old]; kn = [key(x, new_lines) for x in new]
            if ko == kn: return 0
            r = 0
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ko, kn, autojunk=False).get_opcodes():
                if tag == "equal": continue
                O, N = old[i1:i2], new[j1:j2]
                if len(O) == len(N) and all(type(x) is type(y) for x, y in zip(O, N)):
                    for k, (x, y) in enumerate(zip(O, N)): r |= self.note(pair(x, y, old, i1 + k, owner, ctx))
                else:
                    if any(not noexec(x, ctx.imp) for x in O + N) or any(isinstance(x, (ast.Global, ast.Nonlocal)) for x in O + N):
                        state["differs"] = True
                    for k, x in enumerate(O): r |= self.note(gone(x, old, i1 + k, owner, ctx))
                    if N: r |= self.note(inserted(N, old, i1, owner, ctx))
            return r

        r = blocks(old_tree.body, new_tree.body, old_tree, Ctx(True, None))
        if SUBPROC and path.startswith("sqlglot/") and r: r |= self.note(self.subproc)
        return r, state["differs"]

    def py_change(self, path, hunks):
        try:
            old_text = (SRC / path).read_text(encoding="utf-8")
        except OSError:
            return self.all
        old_lines = old_text.split("\n")
        if not hunks_fit(old_lines, hunks): return self.all          # not a patch against the pinned file
        new_lines, removed, anchors = apply_hunks(old_lines, hunks)
        whole = self.file_tests(path) | (self.subproc if SUBPROC and path.startswith("sqlglot/") else 0)
        if path.startswith("tests/") and path in self.by_file: whole |= self.by_file[path]
        old_tree = self.tree_of(path) if self.sources(True).get(path) == old_text else None
        try:
            if old_tree is None: old_tree = ast.parse(old_text)
        except (SyntaxError, ValueError):
            return self.all
        try:
            new_tree = ast.parse("\n".join(new_lines))
        except (SyntaxError, ValueError):
            return self.note(whole)                                   # the file no longer imports
        struct = 0
        if STRUCT != "old":
            struct, differs = self.structural(path, old_tree, new_tree, whole, old_lines, new_lines)
            if not differs:
                # Comments, layout or docstrings only. One comment does change the program: an encoding declaration.
                cookie = lambda ls: [m.group(1) for l in ls[:2] for m in [COOKIE.match(l)] if m]
                return self.note(whole) if cookie(old_lines) != cookie(new_lines) else 0
            if STRUCT == "new": return struct
        return struct | self.py_change_lines(path, old_text, old_lines, new_lines, removed, anchors, whole)

    def py_change_lines(self, path, old_text, old_lines, new_lines, removed, anchors, whole):
        """The older, line-based rules: the tests that execute the changed lines and their nearest neighbours."""
        old_tree = ast.parse(old_text); new_tree = ast.parse("\n".join(new_lines))
        if ast.dump(old_tree) == ast.dump(new_tree):
            return 0
        if strip_docstrings(old_tree) == strip_docstrings(new_tree) and not self.reads_docstrings():
            return 0                           # only docstrings differ, and no numbered test file reads docstrings
        old_tree = ast.parse(old_text)         # (strip_docstrings edits the tree it is given)
        new_tree = ast.parse("\n".join(new_lines))
        idx = Index(old_tree); r = 0
        od, nd = bound_defs(old_tree), bound_defs(new_tree)
        for name in sorted(set(od) - set(nd)):
            if isinstance(od[name], ast.ClassDef): continue      # (naming a class needs this file imported: the class statement's own tests)
            r |= self.note(self.gone_name(name, path, idx) & (self.cover(path, stmt_lo(od[name]), stmt_hdr(od[name])) or whole))
        n = len(old_lines)
        sk_old, fns_old, own_old = strip_bodies(old_tree)
        sk_new, fns_new, own_new = strip_bodies(new_tree)
        bodies_only = sk_old == sk_new and len(fns_old) == len(fns_new)      # every change is inside a function-like body
        if bodies_only:
            for f, a, b in zip(fns_old, own_old, own_new):
                if a != b and isinstance(f, ast.Lambda):
                    r |= self.note(self.fn_tests(path, f.lineno))
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
                r |= self.note(whole); continue          # module level outside any statement
            lo, hi, hdr, node, func, cls = s
            if bodies_only and func is None and not isinstance(node, FUNC):
                continue                       # import-time statement whose only changes are lambda bodies (handled above)
            if not bodies_only and func is None and cls and isinstance(node, (ast.Assign, ast.AnnAssign)):
                r |= self.note(self.class_attr(path, node, cls, whole)); continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and line <= hdr and not node.decorator_list:
                c = self.cover(path, hdr + 1, hi)          # signature change: whoever runs the body
            elif isinstance(node, ast.ClassDef) or func is None:
                c = self.cover(path, lo, hdr) or whole     # import-time code
            else:
                c = self.cover(path, lo, hdr)
            r |= self.note(c)
        if SUBPROC and path.startswith("sqlglot/") and r: r |= self.note(self.subproc)
        return r


# ---- deciding a heavy test by running the cheaper selected tests ---------------------------------------------
# The statically selected tests are a safe superset. When it carries a test of five seconds or more, the cheaper
# selected tests are run inside the container against the patched tree; if they all pass, the heavy test is not
# scheduled. CPU time is charged, so this is done only when the run costs clearly less than the heavy test. Any
# doubt (timeout, crash, a patch that cannot be reproduced exactly) keeps the static selection.

STRUCT = os.environ.get("BR_STRUCT", "new")        # new: statement-level comparison; union: plus the older line-based rules
# A test that starts a Python subprocess is selected through its coverage like any other: the map traces child
# processes, and what the child imports the test's own process imports too. BR_SUBPROC=1 adds such tests to every
# selection for a sqlglot/ change instead.
SUBPROC = os.environ.get("BR_SUBPROC", "0") == "1"
HEAVY_MS = int(os.environ.get("BR_HEAVY_MS", "5000"))
RUN = os.environ.get("BR_RUN", "1") == "1"             # decide heavy tests by running the cheaper selected tests
FORCE = os.environ.get("BR_FORCE", "0") == "1"
MAX_EST = float(os.environ.get("BR_MAX_EST", "14"))    # a run that goes wrong is killed, and its CPU is still charged
HEAVY_RATIO = float(os.environ.get("BR_HEAVY_RATIO", "0.6"))
MIN_WITNESS = int(os.environ.get("BR_WITNESS", "3"))
KEEP_WITNESSES = os.environ.get("BR_KEEP_WITNESSES", "0") == "1"
# The second, cheaper witness set (selected tests outside test_optimizer.py / test_executor.py) is off: on labelled
# generated patches it dropped test_execute_tpcds wrongly 2 times in 13 (optimizer changes that only a TPC-DS query
# shows, with four passing witnesses each).
NONPOOL = os.environ.get("BR_NONPOOL", "0") == "1"
WALL_BUDGET = float(os.environ.get("BR_WALL", "110"))
TMP = os.environ.get("BR_TMP", "/tmp")
POOL_FILES = ("tests/test_optimizer.py", "tests/test_executor.py")


def hunks_fit(old_lines, hunks):
    for start, body in hunks:
        pos = max(start - 1, 0) if any(t != "+" for t, _ in body) else start
        for tag, text in body:
            if tag == "+": continue
            if pos >= len(old_lines) or old_lines[pos] != text: return False
            pos += 1
    return True


def patch_edits(files):
    """{path: new text, or None when deleted} for a patch we can reproduce exactly; None when we cannot."""
    edits = {}
    for f in files:
        old, new, hunks, flags = f[0], f[1], f[2], f[3:]
        if "new file" in flags: old = None
        if "deleted file" in flags: new = None
        if "GIT binary" in flags or "Binary files" in flags or (old is None and new is None): return None
        for p in (old, new):
            if p and (p.startswith("/") or ".." in p.split("/")): return None
        if old is not None:
            try:
                text = edits[old] if old in edits and edits[old] is not None else (SRC / old).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return None
            old_lines = text.split("\n")
        else:
            old_lines = []
        if not hunks_fit(old_lines, hunks): return None
        if new is None:
            edits[old] = None; continue
        new_lines = apply_hunks(old_lines, hunks)[0] if hunks else old_lines
        text = "\n".join(new_lines)
        if old is None and hunks and not text.endswith("\n"): text += "\n"
        if old is not None and old != new: edits[old] = None
        edits[new] = text
    return edits


def build_overlay(root, edits):
    """A copy of the pinned checkout made of symlinks, with the patched files written for real."""
    changed = set(edits)
    def walk(rel):
        src = SRC / rel if rel else SRC; dst = os.path.join(root, rel)
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            r = rel + "/" + name if rel else name
            if r in changed: continue
            if any(c.startswith(r + "/") for c in changed) and os.path.isdir(src / name): walk(r)
            else: os.symlink(src / name, os.path.join(dst, name))
    walk("")
    for p, text in edits.items():
        if text is None: continue
        os.makedirs(os.path.dirname(os.path.join(root, p)), exist_ok=True)
        with open(os.path.join(root, p), "w", encoding="utf-8", newline="") as fh: fh.write(text)


class _Recorder:
    def __init__(self): self.failed, self.seen, self.collect = set(), set(), []
    def pytest_runtest_logreport(self, report):
        self.seen.add(report.nodeid)
        if report.outcome == "failed": self.failed.add(report.nodeid)
    def pytest_collectreport(self, report):
        if report.outcome == "failed": self.collect.append(report.nodeid)


def _child(root, ids, out, tmpd, per_test_timeout):
    try:
        os.setsid()
        nul = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2): os.dup2(nul, fd)
        os.chdir(root)
        try:                                   # if a patched loop eats the memory, this process is the one to go
            with open("/proc/self/oom_score_adj", "w") as fh: fh.write("1000")
        except OSError:
            pass
        import tempfile
        os.environ["TMPDIR"] = tmpd; tempfile.tempdir = None
        here = os.path.dirname(os.path.abspath(__file__))
        sys.path[:] = [root] + [p for p in sys.path if p not in ("", here, str(SRC), root)]
        # One pool worker and one thread per library: the tests' process pools and the numeric libraries would
        # otherwise start one per host CPU, each paying an import and counting against the process limit.
        os.environ.update(PYTHONPATH=root, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1", PYTHON_CPU_COUNT="1",
                          OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", NUMEXPR_NUM_THREADS="1")
        os.cpu_count = lambda: 1
        if hasattr(os, "process_cpu_count"): os.process_cpu_count = lambda: 1
        sys.dont_write_bytecode = True
        import pytest
        rec = _Recorder()
        # -x: one failure already decides (the static selection is kept), so stop there
        code = pytest.main(["-q", "-x", "-p", "no:cacheprovider", "--timeout", str(per_test_timeout), *ids], plugins=[rec])
        with open(out + ".tmp", "w") as fh:
            json.dump({"code": int(code), "failed": sorted(rec.failed), "seen": sorted(rec.seen), "collect": rec.collect}, fh)
        os.rename(out + ".tmp", out)
    finally:
        os._exit(0)


_SEQ = [0]


def run_once(edits, ids, est, deadline, label):
    """Run tests with pytest in a forked child, against the patched tree (edits) or the pinned tree (edits is None).
    -> (failing ids, ids that passed), or None when the run gave no usable answer."""
    if time.time() >= deadline: return None
    try:
        import pytest  # noqa: F401  (imported once here, inherited by every forked run)
        if any(i.split("::")[0] in POOL_FILES for i in ids):
            # These two test files import pandas, duckdb and numpy: seconds of CPU per process. Import them once
            # here, single-threaded so that no thread exists at the fork; sqlglot itself is never imported here.
            os.environ.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", NUMEXPR_NUM_THREADS="1")
            import numpy, pandas, duckdb  # noqa: F401
    except Exception:
        return None
    _SEQ[0] += 1
    base = os.path.join(TMP, f"br-{os.getpid()}-{_SEQ[0]}"); out = base + ".json"; tmpd = base + ".tmp"; root = str(SRC)
    def cleanup():
        if edits is not None: shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(tmpd, ignore_errors=True)
        try: os.remove(out)
        except OSError: pass
    try:
        if edits is not None:
            root = base; shutil.rmtree(root, ignore_errors=True); build_overlay(root, edits)
        os.makedirs(tmpd, exist_ok=True)
        sys.stdout.flush(); sys.stderr.flush()
        pid = os.fork()
    except Exception:
        cleanup(); return None
    if pid == 0: _child(root, ids, out, tmpd, int(max(5, est * 2)))
    limit = time.time() + max(8, est * 2.5 + 4); status = cpu = None      # a patched loop that never ends is cut off here
    while True:
        try:
            p, st, ru = os.wait4(pid, os.WNOHANG)
        except ChildProcessError:
            break
        if p == pid:
            status, cpu = st, ru.ru_utime + ru.ru_stime; break
        if time.time() > limit: break
        time.sleep(0.01)
    try: os.killpg(pid, signal.SIGKILL)                   # the run if it is still going, and any stray pool worker
    except OSError: pass
    try:
        while os.waitpid(-1, 0 if status is None else os.WNOHANG)[0] > 0 and status is not None: pass
    except ChildProcessError:
        pass
    res = None
    try:
        if status is not None and os.WIFEXITED(status) and os.path.exists(out):
            d = json.load(open(out))
            if d["code"] in (0, 1):
                idset = set(ids); bad = set(d["failed"])
                for c in d["collect"]: bad |= {i for i in idset if i.split("::")[0] == c or not c.endswith(".py")}
                if not bad: bad = idset - set(d["seen"])              # (after a failure the run stops, so the rest is unseen)
                res = (bad & idset, (set(d["seen"]) & idset) - bad)
    except Exception:
        res = None
    cleanup()
    print(f"{label}: ran {len(ids)} tests, {'no answer' if res is None else str(len(res[0])) + ' failed'}, cpu {cpu if cpu is None else round(cpu, 2)} s", file=sys.stderr)
    return res


# ---- an exact verdict for two heavy tests, unit by unit ----------------------------------------------------------
# test_generator_memory checks each dialect in its own subtest and test_execute_tpcds each TPC-DS query. A second
# map of the same kind as map.bin (gmap.bin, qmap.bin) records what each unit executes on the pinned commit, so
# the selector can tell which units a patch can affect. It replays just those in the container on the patched
# tree (gm_replay.py, tpcds_replay.py: the test's own steps, without pytest). If every affected unit still gives
# what the test needs, the test cannot fail and is not scheduled; otherwise it stays.
GM_LIMIT = 512                    # bytes; the test asserts under 1,024 and the pinned commit measures 32
EXACT = os.environ.get("BR_EXACT", "1") == "1"
EXACT_RATIO = float(os.environ.get("BR_EXACT_RATIO", "0.7"))
UNIT_TESTS = (("tests/test_generator.py::TestGenerator::test_generator_memory", "gmap.bin", "gm_expected.json", "gm"),
              ("tests/test_executor.py::TestExecutor::test_execute_tpcds", "qmap.bin", "tpcds_expected.json", "tpcds"))


class Units:
    def __init__(self, tests, real, map_name, exp_name, kind):
        here = Path(__file__).parent; self.kind, self.ok = kind, False
        try:
            self.exp = json.loads((here / exp_name).read_text())       # the pinned commit's results and CPU costs, from the image build
            ids = pickle.loads(zlib.decompress((here / map_name).read_bytes()))["ids"]
            self.sel = Selector([{"id": i, "number": k} for k, i in enumerate(ids)], here / map_name)
            self.n = len(ids) - 1                                      # the last id is "harness": what only the real test executes
            self.test = next(t["number"] for t in tests if t["id"] == real)
            self.ok = len(self.exp["cost"]) == self.n
            # emap.bin: the same units, restricted to what runs in a query's data phase (while the executor is built
            # and executes) plus import-time code. A query whose data phase a patch cannot touch needs no execution:
            # it is enough that the plan and tables handed to the executor, and the DuckDB SQL, are unchanged.
            self.esel = Selector([{"id": i, "number": k} for k, i in enumerate(ids)], here / "emap.bin") if kind == "tpcds" and "cdigest" in self.exp and (here / "emap.bin").exists() else None
        except Exception as e:
            print(f"{kind}: unit replay off: {type(e).__name__}: {e}", file=sys.stderr)

    def units(self, patch_text):
        """The units the patch can affect, cheapest first; None when the test cannot be judged unit by unit."""
        b = self.sel.select(patch_text)
        if not b or b == self.sel.all or b >> self.n & 1: return None
        full = lambda u: True
        if getattr(self, "esel", None) is not None:
            e = self.esel.select(patch_text)
            if e != self.esel.all and not e >> self.n & 1: full = lambda u: bool(e >> u & 1)
        return sorted(((u, full(u)) for u in range(self.n) if b >> u & 1), key=self.cost)

    def cost(self, item):
        return self.exp["cost" if item[1] else "ccost"][item[0]]

    def estimate(self, units):
        """units: [(unit, True to run it in full / False to compare only what its data phase is given)]"""
        # (tables are read from csv only when some query is executed; otherwise they come from the build's pickles)
        load = sum(self.exp["load"].get(t, 0) for t in {t for u, _ in units for t in self.exp["tables"][u]}) if self.kind == "tpcds" and any(f for _, f in units) else 0
        return 0.6 + 1.2 * (sum(self.cost(i) for i in units) + load)


def _units_child(root, kind, units, exp, out, tmpd):
    try:
        os.setsid()
        nul = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2): os.dup2(nul, fd)
        os.chdir(root)
        try:
            with open("/proc/self/oom_score_adj", "w") as fh: fh.write("1000")
        except OSError:
            pass
        import tempfile
        os.environ["TMPDIR"] = tmpd; tempfile.tempdir = None
        here = os.path.dirname(os.path.abspath(__file__))
        sys.path[:] = [root, here] + [p for p in sys.path if p not in ("", here, str(SRC), root)]
        os.environ.update(PYTHONPATH=root, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
        sys.dont_write_bytecode = True
        bad = None
        try:
            if kind == "tpcds":
                from tpcds_replay import Replay
                tables = os.path.join(here, "tpcds_expected.json.tables")
                # (prebuilt table pickles only when no query is executed: then no changed code can touch a Table)
                r = Replay(pickled=tables if os.path.exists(tables) and not any(f for _, f in units) else None)
                check = lambda u, full: (r.run(u) == exp["digest"][u]) if full else (r.compile(u) == exp["cdigest"][u])
            else:
                import gm_replay
                check = lambda u, full: gm_replay.run(exp["names"][u]) <= GM_LIMIT
            for u in units:
                if not check(*u): bad = u; break                       # one unit is enough to keep the test
        except BaseException:
            bad = units[0] if bad is None else bad
        with open(out + ".tmp", "w") as fh: json.dump({"bad": bad}, fh)
        os.rename(out + ".tmp", out)
    finally:
        os._exit(0)


def run_units(edits, obj, units, est, deadline, label):
    """Replay units of a heavy test in a forked child on the patched tree (edits) or the pinned tree (edits is None).
    -> (True, None) when every unit is as the test needs, (False, unit) for the first that is not, (None, None)
    for no answer."""
    if time.time() >= deadline: return None, None
    _SEQ[0] += 1
    base = os.path.join(TMP, f"br-{os.getpid()}-{_SEQ[0]}"); out = base + ".json"; tmpd = base + ".tmp"; root = str(SRC)
    def cleanup():
        if edits is not None: shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(tmpd, ignore_errors=True)
        try: os.remove(out)
        except OSError: pass
    try:
        if edits is not None:
            root = base; shutil.rmtree(root, ignore_errors=True); build_overlay(root, edits)
        os.makedirs(tmpd, exist_ok=True)
        sys.stdout.flush(); sys.stderr.flush()
        pid = os.fork()
    except Exception:
        cleanup(); return None, None
    if pid == 0: _units_child(root, obj.kind, units, obj.exp, out, tmpd)
    limit = time.time() + max(8, est * 2.5 + 4); status = cpu = None
    while True:
        try:
            p, st, ru = os.wait4(pid, os.WNOHANG)
        except ChildProcessError:
            break
        if p == pid:
            status, cpu = st, ru.ru_utime + ru.ru_stime; break
        if time.time() > limit: break
        time.sleep(0.01)
    try: os.killpg(pid, signal.SIGKILL)
    except OSError: pass
    try:
        while os.waitpid(-1, 0 if status is None else os.WNOHANG)[0] > 0 and status is not None: pass
    except ChildProcessError:
        pass
    res = (None, None)
    try:
        if status is not None and os.WIFEXITED(status) and os.path.exists(out):
            bad = json.load(open(out))["bad"]
            res = (True, None) if bad is None else (False, bad)
    except Exception:
        res = (None, None)
    cleanup()
    print(f"{label}: replayed {len(units)} {obj.kind} units, {'no answer' if res[0] is None else 'all as on the pinned commit' if res[0] else 'one differs'}, cpu {cpu if cpu is None else round(cpu, 2)} s", file=sys.stderr)
    return res


def pack_cost(numbers, ms):
    if not numbers: return 0
    ws = pack(numbers, ms)
    return max(sum(ms[n] for n in w) for w in ws) + WORKER_MS * len(ws)


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


def run_estimate(wit, ms, tid):
    """CPU seconds a witness run is expected to use on the scorer (measured under amd64 emulation, taken at face value)."""
    files = {tid[n].split("::")[0] for n in wit}
    return (1.3 + (5.5 if "tests/test_executor.py" in files else 0) + (7.0 if "tests/test_optimizer.py" in files else 0)
            + 1.3 * sum(ms[n] for n in wit) / 1000)


def main():
    t0 = time.time()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    tid = {t["number"]: t["id"] for t in tests}; tnum = {t["id"]: t["number"] for t in tests}
    s = Selector(tests); chosen, parts, edits_of, jobs, texts = {}, {}, {}, [], {}
    inv = {n: i for i, n in enumerate(s.remap)} if s.remap else {t["number"]: t["number"] for t in tests}
    def witness_plan(cid, broken):
        """(heavy tests, witnesses to run, estimate, saving) when the commit's heavy tests can be decided by cheaper
        selected tests at a worthwhile cost; (None, ...) otherwise. Two witness sets are tried: every cheaper selected
        test, then only those outside the two test files that need pandas, duckdb and worker processes (the others
        then stay scheduled). Either way every changed spot that selected a heavy test needs MIN_WITNESS witnesses."""
        nums = chosen[cid]
        heavy = [n for n in nums if ms[n] >= HEAVY_MS]
        cheaper = [n for n in nums if ms[n] < HEAVY_MS and tid[n].split("::")[0] not in s.resolvers() and tid[n] not in broken]
        sets = (cheaper, [n for n in cheaper if tid[n].split("::")[0] not in POOL_FILES]) if NONPOOL else (cheaper,)
        for wit in sets if heavy else ():
            wb = sum(1 << inv[n] for n in wit)
            if not wit or not all(bin(p & wb).count("1") >= MIN_WITNESS for p in parts[cid] if any(p >> inv[h] & 1 for h in heavy)): continue
            est = run_estimate(wit, ms, tid)
            after = [n for n in nums if n not in heavy and (KEEP_WITNESSES or n not in wit)]
            saving = (pack_cost(nums, ms) - pack_cost(after, ms)) / 1000
            if FORCE or (est <= MAX_EST and est <= HEAVY_RATIO * saving): return heavy, wit, est, saving
        return None, None, 0, 0
    for patch in sorted((DATA / "commits").glob("*.patch")):
        text = patch.read_text(encoding="utf-8", errors="surrogateescape")
        try:
            b = s.select(text)
        except Exception as e:                                   # never miss because of a bug: run everything
            print(f"{patch.stem}: {type(e).__name__}: {e}", file=sys.stderr); b = s.all
        nums = [i for i in range(b.bit_length()) if b >> i & 1]
        if s.remap: nums = [s.remap[i] for i in nums if s.remap[i] is not None] + (s.unknown if nums else [])
        chosen[patch.stem] = nums
        parts[patch.stem] = list(getattr(s, "parts", []))
        if nums and b != s.all: texts[patch.stem] = text
        if not RUN or not nums or b == s.all or s.remap: continue
        try:
            # Only a selection that carries a heavy test (five seconds or more) is worth a run: the heavy test is
            # dropped when every cheaper selected test passes on the patched tree and at least MIN_WITNESS of them
            # exercise each changed spot that selected it. Cheap selections are scheduled as selected.
            heavy, wit, est, saving = witness_plan(patch.stem, ())
            if not heavy: continue
            files = parse_patch(text)
            if not any((p or "").endswith(".py") for f in files for p in f[:2]): continue
            print(f"{patch.stem}: plan heavy={len(heavy)} witnesses={len(wit)} of {len(nums) - len(heavy)} est={est:.1f} saving={saving:.1f}", file=sys.stderr)
            edits = patch_edits(files)
            if edits is None: continue
            edits_of[patch.stem] = edits; jobs.append((saving - est, patch.stem))
        except Exception as e:
            print(f"{patch.stem}: plan {type(e).__name__}: {e}", file=sys.stderr)
    # Exact verdicts first: a heavy test whose affected units all behave as on the pinned commit is not scheduled.
    exact = [Units(tests, *u) for u in UNIT_TESTS] if EXACT and not s.remap else []
    ejobs = []
    for cid, nums in chosen.items():
        for obj in exact:
            try:
                if not obj.ok or obj.test not in nums or cid not in texts: continue
                us = obj.units(texts[cid])
                if us is None: continue
                est = obj.estimate(us)
                saving = (pack_cost(nums, ms) - pack_cost([n for n in nums if n != obj.test], ms)) / 1000
                print(f"{cid}: {obj.kind} affects {len(us)} of {obj.n} units ({sum(1 for _, f in us if f)} to run in full) est={est:.1f} saving={saving:.1f}", file=sys.stderr)
                if est <= MAX_EST and est <= EXACT_RATIO * saving: ejobs.append((saving - est, cid, obj, us, est))
            except Exception as e:
                print(f"{cid}: units {type(e).__name__}: {e}", file=sys.stderr)
    for _, cid, obj, us, est in sorted(ejobs, key=lambda j: -j[0]):
        try:
            if not obj.ok: continue
            if cid not in edits_of: edits_of[cid] = patch_edits(parse_patch(texts[cid]))
            if edits_of[cid] is None: continue
            same, bad = run_units(edits_of[cid], obj, us, est, t0 + WALL_BUDGET, cid)
            if same:
                chosen[cid] = [n for n in chosen[cid] if n != obj.test]
                print(f"{cid}: {obj.kind} cannot fail: not scheduled", file=sys.stderr)
            elif same is False and run_units(None, obj, [tuple(bad)], obj.estimate([tuple(bad)]), t0 + WALL_BUDGET, cid + " (unpatched)")[0] is False:
                obj.ok = False               # the unit differs here even without the patch: this container cannot replay it
        except Exception as e:
            print(f"{cid}: replay {type(e).__name__}: {e}", file=sys.stderr)
    broken = set()                 # tests that fail here on the unpatched tree: this container cannot judge them
    proven = set()                 # tests that have passed here at least once: this container can run them
    for _, cid in sorted(jobs, reverse=True):
        try:
            deadline = t0 + WALL_BUDGET
            heavy, wit, est, _ = witness_plan(cid, broken)
            if not heavy or edits_of.get(cid) is None: continue
            ids = [tid[n] for n in wit]
            res = run_once(edits_of[cid], ids, est, deadline, cid)
            if res is None: continue                                     # no answer: keep the static selection
            bad, passed = res; proven |= passed
            if bad:
                # A failure counts only if the same test can pass in this container: it has passed here before, or
                # it passes now on the unpatched tree. Otherwise the failure is the container's doing: the test
                # keeps its static decision and is not run again.
                if bad & proven: continue                                # a real failure: keep the static selection
                res = run_once(None, sorted(bad), run_estimate([tnum[i] for i in bad], ms, tid), deadline, cid + " (unpatched)")
                if res is None: continue
                again, passed = res; proven |= passed; broken |= again
                if bad - again: continue                                 # a real failure: keep the static selection
                ran = set(wit)
                heavy, wit, _, _ = witness_plan(cid, broken)             # the rule must still hold without the unjudged tests
                if not heavy or not set(wit) <= ran: continue
            dropped = set(heavy) | (set() if KEEP_WITNESSES else set(wit))
            chosen[cid] = [n for n in chosen[cid] if n not in dropped]
            print(f"{cid}: {len(wit)} cheaper tests pass: dropped {len(heavy)} heavy, scheduling {len(chosen[cid])}", file=sys.stderr)
        except Exception as e:
            print(f"{cid}: run {type(e).__name__}: {e}", file=sys.stderr)
    out = []
    for cid in sorted(chosen):
        print(f"{cid}: {len(chosen[cid])} tests", file=sys.stderr)
        out.append(json.dumps({"commit": cid, "workers": pack(chosen[cid], ms)}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
