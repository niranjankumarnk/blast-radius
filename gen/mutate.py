"""Generate varied patches against the pinned sqlglot checkout, for evaluating the selector's recall.

usage: mutate.py SRC OUTDIR N [SEED]
Writes OUTDIR/<id>.patch and OUTDIR/kinds.json. Evaluation only: nothing here ships in the image.
"""
import ast, difflib, json, os, random, re, sys

SRC, OUT, N = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 1)
os.makedirs(OUT, exist_ok=True)


def ls(prefix, suffix=""):
    out = []
    for d, dirs, fs in os.walk(os.path.join(SRC, prefix)):
        dirs[:] = [x for x in dirs if x not in ("__pycache__", ".git")]
        for f in fs:
            if f.endswith(suffix) and not f.endswith(".pyc"):
                out.append(os.path.relpath(os.path.join(d, f), SRC))
    return sorted(out)


PY_SRC = ls("sqlglot", ".py")
PY_TEST = [p for p in ls("tests", ".py")]
FIXTURES = [p for p in ls("tests/fixtures") if not p.endswith(".gz")]
GZ = [p for p in ls("tests/fixtures") if p.endswith(".gz")]
OTHER = [p for p in ("README.md", "pyproject.toml", "setup.cfg", "setup.py", "Makefile", "MANIFEST.in", "CHANGELOG.md", "sqlglot/py.typed",
                     ".pre-commit-config.yaml", "LICENSE", "CONTRIBUTING.md", ".gitignore", "pdoc/templates/module.html.jinja2")
         if os.path.exists(os.path.join(SRC, p))] + ls("sqlglotc") + ls("benchmarks") + ls("pdoc", ".py")
_cache = {}


def read(p):
    if p not in _cache:
        with open(os.path.join(SRC, p), encoding="utf-8", newline="") as f: _cache[p] = f.read()
    return _cache[p]


def tree(p): return ast.parse(read(p))


def seg(lines, n):
    """(start, end) character offsets of node n in the joined text; None when a line is not ASCII."""
    for i in range(n.lineno - 1, n.end_lineno):
        if not lines[i].isascii(): return None
    off = sum(len(l) + 1 for l in lines[: n.lineno - 1])
    off2 = sum(len(l) + 1 for l in lines[: n.end_lineno - 1])
    return off + n.col_offset, off2 + n.end_col_offset


def parents(t):
    for n in ast.walk(t):
        for c in ast.iter_child_nodes(n): c._p = n
    return t


def scope_of(n):
    """'func', 'class' or 'module': where the statement holding n executes."""
    while hasattr(n, "_p"):
        n = n._p
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)): return "func"
        if isinstance(n, ast.ClassDef): return "class"
    return "module"


def is_doc(n):
    p = getattr(n, "_p", None)
    return isinstance(p, ast.Expr)


def pick(xs):
    return rng.choice(xs) if xs else None


# ---- single-file python mutations: each returns new text or None ----------------------------------------------

def m_cmp(p, where=None):
    text = read(p); lines = text.split("\n"); t = parents(tree(p)); c = []
    swap = {"==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">", "is not": "is", "is": "is not", "not in": "in", "in": "not in"}
    for n in ast.walk(t):
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and (where is None or scope_of(n) == where):
            a, b = seg(lines, n.left), seg(lines, n.comparators[0])
            if a and b: c.append((a[1], b[0]))
    if not c: return None
    s, e = pick(c); mid = text[s:e]
    for k in sorted(swap, key=len, reverse=True):
        m = re.search(r"(?<![=!<>\w])" + re.escape(k) + r"(?![=\w])", mid)
        if m: return text[:s] + mid[: m.start()] + swap[k] + mid[m.end():] + text[e:]
    return None


def m_bool(p, where=None):
    text = read(p); lines = text.split("\n"); t = parents(tree(p)); c = []
    for n in ast.walk(t):
        if isinstance(n, ast.BoolOp) and (where is None or scope_of(n) == where):
            i = rng.randrange(len(n.values) - 1); a, b = seg(lines, n.values[i]), seg(lines, n.values[i + 1])
            if a and b: c.append((a[1], b[0], "or" if isinstance(n.op, ast.And) else "and"))
    if not c: return None
    s, e, new = pick(c); mid = text[s:e]
    m = re.search(r"\b(and|or)\b", mid)
    return text[:s] + mid[: m.start()] + new + mid[m.end():] + text[e:] if m else None


def m_const(p, where=None):
    text = read(p); lines = text.split("\n"); t = parents(tree(p)); c = []
    for n in ast.walk(t):
        if isinstance(n, ast.Constant) and not is_doc(n) and (where is None or scope_of(n) == where):
            s = seg(lines, n)
            if not s or n.lineno != n.end_lineno: continue
            v = n.value
            if v is True: new = "False"
            elif v is False: new = "True"
            elif isinstance(v, int): new = str(v + 1)
            elif isinstance(v, str) and v and text[s[0]] in "\"'" and text[s[0]:s[0] + 3] not in ('"""', "'''") and "\\" not in text[s[0]:s[1]]:
                q = text[s[0]]; new = q + (v[:-1] if rng.random() < 0.5 and len(v) > 1 else v + "X").replace(q, "") + q
            elif v is None: new = "0"
            else: continue
            c.append((s, new))
    if not c: return None
    (s, e), new = pick(c)
    return text[:s] + new + text[e:]


def m_delstmt(p, where="func"):
    text = read(p); lines = text.split("\n"); t = parents(tree(p)); c = []
    for n in ast.walk(t):
        if isinstance(n, (ast.Expr, ast.Assign, ast.AugAssign, ast.Return, ast.Raise, ast.AnnAssign)) and not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)):
            if scope_of(n) != where: continue
            body = [b for f in ("body", "orelse", "finalbody") for b in getattr(n._p, f, []) or []]
            c.append((n, len([b for b in getattr(n._p, "body", [])]) == 1 or len(body) == 1))
    if not c: return None
    n, only = pick(c)
    ind = lines[n.lineno - 1][: n.col_offset]
    if ind.strip(): return None
    new = lines[: n.lineno - 1] + ([ind + "pass"] if only else []) + lines[n.end_lineno:]
    return "\n".join(new)


def m_retnone(p):
    text = read(p); lines = text.split("\n"); c = []
    for n in ast.walk(tree(p)):
        if isinstance(n, ast.Return) and n.value is not None:
            s = seg(lines, n.value)
            if s: c.append(s)
    if not c: return None
    s, e = pick(c); return text[:s] + "None" + text[e:]


def m_default(p):
    text = read(p); lines = text.split("\n"); c = []
    for n in ast.walk(tree(p)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.args.defaults + [x for x in n.args.kw_defaults if x is not None]:
                if isinstance(d, ast.Constant) and d.lineno == d.end_lineno:
                    s = seg(lines, d)
                    if s: c.append((s, d.value))
    if not c: return None
    (s, e), v = pick(c)
    new = "False" if v is True else "True" if v is False else "0" if v is None else str(v + 1) if isinstance(v, int) else '"zz"'
    return text[:s] + new + text[e:]


def m_signature(p):
    """Rename a parameter, or drop one that has a default."""
    text = read(p); lines = text.split("\n"); c = []
    for n in ast.walk(tree(p)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for a in n.args.args + n.args.kwonlyargs:
                if a.arg not in ("self", "cls") and a.lineno == a.end_lineno:
                    s = seg(lines, a)
                    if s: c.append((s, a.arg))
    if not c: return None
    (s, e), name = pick(c)
    return text[:s] + name + "_renamed" + text[s + len(name):]


def m_classattr(p):
    """Change something inside a class-level assignment: a constant, or drop one line of a multi-line literal."""
    text = read(p); lines = text.split("\n"); t = parents(tree(p)); c = []
    for n in ast.walk(t):
        if isinstance(n, ast.ClassDef):
            for b in n.body:
                if isinstance(b, (ast.Assign, ast.AnnAssign)) and b.value is not None: c.append(b)
    if not c: return None
    b = pick(c)
    if b.end_lineno - b.lineno >= 2 and rng.random() < 0.5:
        cand = [i for i in range(b.lineno, b.end_lineno - 1) if lines[i].rstrip().endswith(",") and lines[i].count("(") == lines[i].count(")") and lines[i].count("[") == lines[i].count("]") and lines[i].count("{") == lines[i].count("}")]
        if cand:
            i = pick(cand); return "\n".join(lines[:i] + lines[i + 1:])
    consts = [x for x in ast.walk(b.value) if isinstance(x, ast.Constant) and x.lineno == x.end_lineno and seg(lines, x)]
    names = [x for x in ast.walk(b.value) if isinstance(x, (ast.Attribute, ast.Name)) and x.lineno == x.end_lineno and seg(lines, x)]
    if consts and (rng.random() < 0.6 or not names):
        x = pick(consts); s, e = seg(lines, x); v = x.value
        new = "False" if v is True else "True" if v is False else "0" if v is None else str(v + 1) if isinstance(v, int) else repr(str(v) + "X") if isinstance(v, str) else None
        return text[:s] + new + text[e:] if new else None
    if names:
        x, y = pick(names), pick(names)
        (s, e), (s2, e2) = seg(lines, x), seg(lines, y)
        if text[s:e] != text[s2:e2]: return text[:s] + text[s2:e2] + text[e:]
    return None


def m_modconst(p):
    text = read(p); lines = text.split("\n"); c = []
    for b in tree(p).body:
        if isinstance(b, (ast.Assign, ast.AnnAssign)) and b.value is not None:
            c += [x for x in ast.walk(b.value) if isinstance(x, ast.Constant) and x.lineno == x.end_lineno and seg(lines, x) and isinstance(x.value, (int, str, bool))]
            if b.end_lineno - b.lineno >= 2:
                c += [("line", i) for i in range(b.lineno, b.end_lineno - 1) if lines[i].rstrip().endswith(",") and all(lines[i].count(a) == lines[i].count(z) for a, z in ("()", "[]", "{}"))]
    if not c: return None
    x = pick(c)
    if isinstance(x, tuple): return "\n".join(lines[: x[1]] + lines[x[1] + 1:])
    s, e = seg(lines, x); v = x.value
    new = "False" if v is True else "True" if v is False else str(v + 1) if isinstance(v, int) else repr(v + "X")
    return text[:s] + new + text[e:]


def m_decorator(p):
    lines = read(p).split("\n"); c = []
    for n in ast.walk(tree(p)):
        for d in getattr(n, "decorator_list", []):
            if d.lineno == d.end_lineno: c.append(d.lineno)
    if not c: return None
    i = pick(c); return "\n".join(lines[: i - 1] + lines[i:])


def m_import(p):
    lines = read(p).split("\n"); t = parents(tree(p)); c = []
    for n in ast.walk(t):
        if isinstance(n, (ast.Import, ast.ImportFrom)): c.append(n)
    if not c: return None
    n = pick(c); r = rng.random()
    if n.lineno == n.end_lineno and len(n.names) == 1 or r < 0.4:
        body = getattr(n._p, "body", [])
        ind = lines[n.lineno - 1][: n.col_offset]
        return "\n".join(lines[: n.lineno - 1] + ([ind + "pass"] if len(body) == 1 else []) + lines[n.end_lineno:])
    if n.end_lineno > n.lineno:                                  # drop one name of a parenthesised import
        cand = [i for i in range(n.lineno, n.end_lineno - 1) if lines[i].strip().endswith(",")]
        if cand:
            i = pick(cand); return "\n".join(lines[:i] + lines[i + 1:])
    a = pick(n.names)
    return None if a.lineno != a.end_lineno else "\n".join(lines[: a.lineno - 1] + [lines[a.lineno - 1][: a.col_offset] + (a.name + "_zz") + lines[a.lineno - 1][a.col_offset + len(a.name):]] + lines[a.lineno:])


def m_funcdel(p):
    lines = read(p).split("\n"); t = parents(tree(p)); c = []
    for n in ast.walk(t):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and len(getattr(n._p, "body", [])) > 1: c.append(n)
    if not c: return None
    n = pick(c); lo = min([n.lineno] + [d.lineno for d in n.decorator_list])
    return "\n".join(lines[: lo - 1] + lines[n.end_lineno:])


def m_funcrename(p):
    text = read(p); lines = text.split("\n"); c = []
    for n in ast.walk(tree(p)):
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)): c.append(n)
    if not c: return None
    n = pick(c); l = lines[n.lineno - 1]
    m = re.match(r"(\s*(?:async\s+)?(?:def|class)\s+)(\w+)", l)
    if not m: return None
    return "\n".join(lines[: n.lineno - 1] + [m.group(1) + m.group(2) + "_renamed" + l[m.end():]] + lines[n.lineno:])


def m_comment(p):
    lines = read(p).split("\n"); c = [i for i, l in enumerate(lines) if l.strip() and not l.strip().startswith(("#", '"""', "'''"))]
    i = pick(c); ind = re.match(r"\s*", lines[i]).group(0)
    r = rng.random()
    if r < 0.4: return "\n".join(lines[:i] + [ind + "# note: intentionally left as is"] + lines[i:])
    if r < 0.6: return "\n".join(lines[:i] + [""] + lines[i:])
    if r < 0.8: return "\n".join(lines[:i] + [lines[i] + "  # reviewed"] + lines[i + 1:])
    return "\n".join(lines[:i] + [lines[i] + " "] + lines[i + 1:])


def m_docstring(p):
    text = read(p); lines = text.split("\n"); c = []
    for n in ast.walk(tree(p)):
        if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Module, ast.AsyncFunctionDef)) and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant) and isinstance(n.body[0].value.value, str):
            c.append(n.body[0].value)
    if not c: return None
    d = pick(c); i = rng.randrange(d.lineno - 1, d.end_lineno)
    l = lines[i]; words = [m for m in re.finditer(r"[A-Za-z]{4,}", l)]
    if not words: return None
    m = pick(words)
    return "\n".join(lines[:i] + [l[: m.start()] + m.group(0)[::-1] + l[m.end():]] + lines[i + 1:])


def m_annotation(p):
    text = read(p); lines = text.split("\n"); c = []
    for n in ast.walk(tree(p)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.returns is not None and seg(lines, n.returns): c.append(seg(lines, n.returns))
        if isinstance(n, ast.arg) and n.annotation is not None and seg(lines, n.annotation): c.append(seg(lines, n.annotation))
        if isinstance(n, ast.AnnAssign) and seg(lines, n.annotation): c.append(seg(lines, n.annotation))
    if not c: return None
    s, e = pick(c); return text[:s] + pick(["int", "t.Any", "str", "object"]) + text[e:]


def m_syntax(p):
    lines = read(p).split("\n"); c = [i for i, l in enumerate(lines) if l.strip().endswith(":")]
    if not c: return None
    i = pick(c); return "\n".join(lines[:i] + [lines[i].rstrip()[:-1]] + lines[i + 1:])


def m_raise_top(p):
    """A statement added at module level (import time)."""
    lines = read(p).split("\n"); t = tree(p)
    body = [b for b in t.body if not isinstance(b, (ast.Import, ast.ImportFrom))]
    if not body: return None
    b = pick(body); new = pick(["_added_at_import = 1 / 0", "import does_not_exist_zz", "_added_at_import = 1", "raise RuntimeError('added')"])
    return "\n".join(lines[: b.lineno - 1] + [new] + lines[b.lineno - 1:])


def m_textline(p):
    """Non-Python text file: change, delete or duplicate one non-blank line."""
    lines = read(p).split("\n"); c = [i for i, l in enumerate(lines) if l.strip()]
    if not c: return None
    i = pick(c); r = rng.random(); l = lines[i]
    if r < 0.5:
        words = list(re.finditer(r"[A-Za-z_]{2,}|\d+", l))
        if not words: return None
        m = pick(words); new = l[: m.start()] + (str(int(m.group(0)) + 1) if m.group(0).isdigit() else m.group(0) + "x") + l[m.end():]
        return "\n".join(lines[:i] + [new] + lines[i + 1:])
    if r < 0.75: return "\n".join(lines[:i] + lines[i + 1:])
    if r < 0.9: return "\n".join(lines[:i] + [l, l] + lines[i + 1:])
    return "\n".join(lines[:i] + [""] + lines[i:])


def m_testassert(p):
    """Change an expected value inside a test file."""
    return m_const(p, "func") or m_cmp(p)


def m_lambda(p):
    text = read(p); lines = text.split("\n"); c = []
    for n in ast.walk(tree(p)):
        if isinstance(n, ast.Lambda):
            for x in ast.walk(n.body):
                if isinstance(x, ast.Constant) and x.lineno == x.end_lineno and seg(lines, x) and isinstance(x.value, (bool, int, str)) and "\\" not in text[seg(lines, x)[0]:seg(lines, x)[1]]:
                    c.append((seg(lines, x), x.value))
                elif isinstance(x, ast.Attribute) and x.lineno == x.end_lineno and seg(lines, x):
                    c.append((seg(lines, x), None))
    if not c: return None
    (s_, e), v = pick(c)
    new = "None" if v is None else "False" if v is True else "True" if v is False else str(v + 1) if isinstance(v, int) else repr(v + "X")
    return text[:s_] + new + text[e:]


PY_OPS = {
    "lambda": m_lambda,
    "cmp": m_cmp, "bool": m_bool, "const": m_const, "delstmt": m_delstmt, "retnone": m_retnone, "default": m_default,
    "signature": m_signature, "classattr": m_classattr, "modconst": m_modconst, "decorator": m_decorator, "import": m_import,
    "funcdel": m_funcdel, "funcrename": m_funcrename, "comment": m_comment, "docstring": m_docstring, "annotation": m_annotation,
    "syntax": m_syntax, "import_time": m_raise_top,
    "classconst": lambda p: m_const(p, "class"), "moduleconst2": lambda p: m_const(p, "module"),
    "classstmt": lambda p: m_delstmt(p, "class"), "modulestmt": lambda p: m_delstmt(p, "module"),
}
INIT = [p for p in PY_SRC if p.endswith("__init__.py")]
CORE = [p for p in PY_SRC if os.path.basename(p) in ("dialect.py", "generator.py", "parser.py", "tokens.py", "helper.py", "errors.py", "schema.py", "trie.py", "time.py", "transforms.py", "jsonpath.py", "serde.py", "lineage.py", "diff.py", "planner.py")]


def diff_file(path, old, new):
    if old == new: return None
    a = old.split("\n") if old is not None else []; b = new.split("\n") if new is not None else []
    eof_a = old is not None and not old.endswith("\n"); eof_b = new is not None and not new.endswith("\n")
    if old is not None and old.endswith("\n"): a = a[:-1]
    if new is not None and new.endswith("\n"): b = b[:-1]
    out = [f"diff --git a/{path} b/{path}"]
    if old is None: out.append("new file mode 100644")
    if new is None: out.append("deleted file mode 100644")
    body = list(difflib.unified_diff(a, b, "/dev/null" if old is None else "a/" + path, "/dev/null" if new is None else "b/" + path, lineterm="", n=3))
    if not body: return None
    res = []
    for k, l in enumerate(body):
        res.append(l)
    # "\ No newline at end of file" markers
    text = "\n".join(out + res) + "\n"
    if eof_a or eof_b:
        return None                      # keep the generator simple: skip files without a trailing newline
    return text


FOCUS = os.environ.get("MUT_FILES"); FOCUS_OPS = [o for o in os.environ.get("MUT_OPS", "").split(",") if o]


def gen_one():
    if FOCUS:
        p = pick([x for x in PY_SRC if re.search(FOCUS, x)]); op = pick(FOCUS_OPS or list(PY_OPS)); new = PY_OPS[op](p)
        return ("focus:" + op, diff_file(p, read(p), new)) if new is not None else None
    kind = rng.choices(
        ["py", "py_core", "py_init", "test_py", "conftest", "fixture", "other", "newfile", "delfile", "rename", "gz", "multi"],
        [46, 10, 5, 7, 1, 7, 5, 5, 4, 3, 1, 6])[0]
    if kind in ("py", "py_core", "py_init"):
        p = pick(PY_SRC if kind == "py" else CORE if kind == "py_core" else INIT)
        op = pick(list(PY_OPS)); new = PY_OPS[op](p)
        if new is None: return None
        return kind + ":" + op, diff_file(p, read(p), new)
    if kind == "test_py":
        p = pick(PY_TEST); op = pick(["testassert", "comment", "delstmt", "import", "funcdel", "modconst", "funcrename", "syntax", "classconst", "decorator"])
        new = (m_testassert if op == "testassert" else PY_OPS[op])(p)
        return (kind + ":" + op, diff_file(p, read(p), new)) if new is not None else None
    if kind == "conftest":
        r = rng.random()
        if r < 0.5: return kind + ":new", diff_file("conftest.py", None, "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef _added():\n    yield\n")
        if r < 0.8: return kind + ":tests_new_raise", diff_file("tests/conftest.py", None, "raise RuntimeError('boom')\n")
        return kind + ":tests_new", diff_file("tests/conftest.py", None, "import os\nos.environ['SQLGLOT_ADDED'] = '1'\n")
    if kind == "fixture":
        p = pick(FIXTURES); new = m_textline(p)
        return (kind + ":" + os.path.splitext(p)[1], diff_file(p, read(p), new)) if new is not None else None
    if kind == "other":
        p = pick(OTHER)
        new = (pick([m_comment, m_const, m_delstmt]) if p.endswith(".py") else m_textline)(p)
        return (kind + ":" + p, diff_file(p, read(p), new)) if new is not None else None
    if kind == "newfile":
        r = rng.random()
        if r < 0.25: p, body = "sqlglot/dialects/zzdialect.py", "from sqlglot.dialects.dialect import Dialect\n\n\nclass ZZDialect(Dialect):\n    pass\n"
        elif r < 0.4: p, body = "sqlglot/zz_helper.py", "VALUE = 1\n"
        elif r < 0.55: p, body = "tests/test_zz_added.py", "def test_added():\n    assert 1 == 2\n"
        elif r < 0.7: p, body = "tests/fixtures/optimizer/zz_added.sql", "SELECT 1;\nSELECT 2;\n"
        elif r < 0.8: p, body = "tests/dialects/test_zz_added.py", "from tests.dialects.test_dialect import Validator\n\n\nclass TestZZ(Validator):\n    dialect = 'duckdb'\n\n    def test_zz(self):\n        self.validate_identity('SELECT 1')\n"
        elif r < 0.9: p, body = "sqlglot/optimizer/zz_rule.py", "def zz_rule(expression):\n    return expression\n"
        else: p, body = "docs/zz.md", "# added\n"
        return kind + ":" + p, diff_file(p, None, body)
    if kind == "delfile":
        p = pick(rng.choice([PY_SRC, FIXTURES, PY_TEST, OTHER]))
        if not read(p).endswith("\n") or not read(p): return None
        return kind + ":" + os.path.dirname(p), diff_file(p, read(p), None)
    if kind == "rename":
        p = pick(rng.choice([PY_SRC, FIXTURES, PY_TEST]))
        base, ext = os.path.splitext(p); q = base + "_moved" + ext
        return kind + ":" + os.path.dirname(p), f"diff --git a/{p} b/{q}\nsimilarity index 100%\nrename from {p}\nrename to {q}\n"
    if kind == "gz":
        p = pick(GZ)
        return kind, f"diff --git a/{p} b/{p}\ndeleted file mode 100644\nindex 1111111..0000000\nBinary files a/{p} and /dev/null differ\n" if False else None
    if kind == "multi":
        parts, seen, kinds = [], set(), []
        for _ in range(rng.randint(2, 4)):
            g = gen_one()
            if not g or not g[1] or g[0].startswith(("multi", "rename", "delfile", "newfile", "conftest")): continue
            path = g[1].split("\n")[0]
            if path in seen: continue
            seen.add(path); parts.append(g[1]); kinds.append(g[0])
        return ("multi:" + "+".join(kinds), "".join(parts)) if len(parts) >= 2 else None
    return None


kinds, n, tries = {}, 0, 0
while n < N and tries < N * 50:
    tries += 1
    try:
        g = gen_one()
    except (SyntaxError, IndexError, ValueError, TypeError, UnicodeDecodeError):
        continue
    if not g or not g[1]: continue
    cid = f"m{n:04d}"
    with open(os.path.join(OUT, cid + ".patch"), "w", encoding="utf-8", newline="") as f: f.write(g[1])
    kinds[cid] = g[0]; n += 1
json.dump(kinds, open(os.path.join(OUT, "kinds.json"), "w"), indent=0)
print(n, "patches;", tries, "tries")
