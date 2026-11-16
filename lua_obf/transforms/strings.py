"""String-literal encryption pass.

Every string literal that appears in an *expression* position (not a table
field name, not a `.foo` key, not a `:method` name — those live in the AST
as plain strings/field metadata, never as `String` expression nodes, so
they're untouched by construction) is pulled out into a byte table and
replaced with a lookup into a table that gets decoded once at script start.

Cipher: for each string we pick a random per-string salt; byte i (1-based,
starting at 2 since slot 1 holds the salt) is encoded as
    enc = (orig + (K + salt) % 256 + i) % 256
and decoded with the inverse. K is a per-build random global key. This is a
simple additive stream cipher — not cryptographic, but it defeats a plain
`strings`-style scan of the output, which is the actual goal here (this
protects a script's own call-site names/URLs/config from casual skimming,
not an adversarial attacker with a debugger).
"""

import random

from ..astnodes import N
from ..parser import parse


def _random_name():
    return "_" + "".join(random.choices("0123456789abcdef", k=8))


class StringEncryptor:
    def __init__(self):
        self.pool = []  # list of (salt, enc_bytes)
        self.key = random.randint(1, 250)
        self.table_name = _random_name()
        self.decode_fn = _random_name()
        self.key_name = _random_name()

    def _encode(self, s):
        salt = random.randint(0, 255)
        kk = (self.key + salt) % 256
        enc = []
        for j, ch in enumerate(s):
            i = j + 2
            b = ord(ch) & 0xFF
            enc.append((b + kk + i) % 256)
        idx = len(self.pool)
        self.pool.append((salt, enc))
        return idx + 1  # Lua 1-based

    # -- entry -------------------------------------------------------------
    def run(self, chunk, min_len=1):
        self.min_len = min_len
        chunk.body = self.visit_block(chunk.body)
        if not self.pool:
            return chunk
        header = self._build_header()
        chunk.body = header + chunk.body
        return chunk

    def _build_header(self):
        parts = []
        for salt, enc in self.pool:
            nums = ",".join(str(b) for b in enc)
            parts.append(f"{{{salt}{',' if nums else ''}{nums}}}")
        table_lit = "{" + ",".join(parts) + "}"
        src = (
            f"local {self.key_name} = {self.key}\n"
            f"local function {self.decode_fn}(t)\n"
            f"  local salt = t[1]\n"
            f"  local kk = ({self.key_name} + salt) % 256\n"
            f"  local out = {{}}\n"
            f"  for i = 2, #t do\n"
            f"    out[i - 1] = string.char((t[i] - kk - i) % 256)\n"
            f"  end\n"
            f"  return table.concat(out)\n"
            f"end\n"
            f"local {self.table_name} = {{}}\n"
            f"do\n"
            f"  local _raw = {table_lit}\n"
            f"  for i = 1, #_raw do\n"
            f"    {self.table_name}[i] = {self.decode_fn}(_raw[i])\n"
            f"  end\n"
            f"end\n"
        )
        return parse(src).body

    # -- generic block/stmt walk (replaces expr fields in place) -----------
    def visit_block(self, stmts):
        return [self.visit_stmt(s) for s in stmts]

    def visit_stmt(self, s):
        k = s.kind
        if k == "LocalStmt":
            s.exprs = [self.visit_expr(e) for e in s.exprs]
        elif k == "AssignStmt":
            s.targets = [self.visit_expr(t) for t in s.targets]
            s.exprs = [self.visit_expr(e) for e in s.exprs]
        elif k == "CallStmt":
            s.call = self.visit_expr(s.call)
        elif k == "DoStmt":
            s.body = self.visit_block(s.body)
        elif k == "WhileStmt":
            s.cond = self.visit_expr(s.cond)
            s.body = self.visit_block(s.body)
        elif k == "RepeatStmt":
            s.body = self.visit_block(s.body)
            s.cond = self.visit_expr(s.cond)
        elif k == "IfStmt":
            s.clauses = [(self.visit_expr(c), self.visit_block(b)) for c, b in s.clauses]
            if s.else_body is not None:
                s.else_body = self.visit_block(s.else_body)
        elif k == "NumericForStmt":
            s.start = self.visit_expr(s.start)
            s.stop = self.visit_expr(s.stop)
            if s.step is not None:
                s.step = self.visit_expr(s.step)
            s.body = self.visit_block(s.body)
        elif k == "GenericForStmt":
            s.exprs = [self.visit_expr(e) for e in s.exprs]
            s.body = self.visit_block(s.body)
        elif k == "FunctionDeclStmt":
            s.body = self.visit_block(s.body)
        elif k == "LocalFunctionStmt":
            s.body = self.visit_block(s.body)
        elif k == "ReturnStmt":
            s.exprs = [self.visit_expr(e) for e in s.exprs]
        elif k in ("BreakStmt", "ContinueStmt", "LabelStmt", "GotoStmt"):
            pass
        else:
            raise ValueError(f"strings: unknown stmt kind {k}")
        return s

    def visit_expr(self, e):
        k = e.kind
        if k == "String":
            if len(e.value) < self.min_len:
                return e
            idx = self._encode(e.value)
            return N("Index", obj=N("Name", name=self.table_name), key=N("Number", raw=str(idx)), dot=False)
        if k in ("Nil", "True", "False", "Vararg", "Number", "Name"):
            return e
        if k == "Paren":
            e.expr = self.visit_expr(e.expr)
            return e
        if k == "Index":
            e.obj = self.visit_expr(e.obj)
            if not e.dot:
                e.key = self.visit_expr(e.key)
            return e
        if k == "Call":
            e.func = self.visit_expr(e.func)
            e.args = [self.visit_expr(a) for a in e.args]
            return e
        if k == "MethodCall":
            e.obj = self.visit_expr(e.obj)
            e.args = [self.visit_expr(a) for a in e.args]
            return e
        if k == "FunctionExpr":
            e.body = self.visit_block(e.body)
            return e
        if k == "TableExpr":
            for f in e.fields:
                if f.ftype == "indexed":
                    f.key = self.visit_expr(f.key)
                f.value = self.visit_expr(f.value)
            return e
        if k == "BinOp":
            e.left = self.visit_expr(e.left)
            e.right = self.visit_expr(e.right)
            return e
        if k == "UnOp":
            e.operand = self.visit_expr(e.operand)
            return e
        raise ValueError(f"strings: unknown expr kind {k}")


def encrypt_strings(chunk, min_len=1):
    return StringEncryptor().run(chunk, min_len=min_len)
