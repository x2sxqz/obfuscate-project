"""Identifier-renaming pass.

Walks the AST with a real lexical-scope model (one scope per block/function,
matching Lua's actual scoping rules) and renames every *local* variable,
function parameter, and `local function` to an obfuscated name. Globals
(anything never introduced by `local`, a function parameter, or a loop
variable — e.g. `print`, `game`, `workspace`, module tables like `string`)
are left completely untouched, since renaming them would change behavior.

Table/field keys (`.foo`, `{foo = 1}`, `:method(...)`) are never touched:
those live in the AST as plain strings, not `Name` nodes, so they're outside
this pass's reach by construction.
"""

LUA_KEYWORDS = {
    "and", "break", "do", "else", "elseif", "end", "false", "for",
    "function", "if", "in", "local", "nil", "not", "or", "repeat",
    "return", "then", "true", "until", "while", "continue",
}


def confusable_name(n, minlen=8):
    """Generate a name built only from 'l' and 'I' — visually near-identical
    in most fonts, which makes renamed code painful to read without tooling."""
    b = format(n, f"0{minlen}b")
    return "".join("l" if c == "0" else "I" for c in b)


def hex_name(n):
    return f"_0x{n:04x}"


class Scope:
    __slots__ = ("parent", "map")

    def __init__(self, parent=None):
        self.parent = parent
        self.map = {}

    def declare(self, orig):
        raise NotImplementedError

    def resolve(self, orig):
        s = self
        while s is not None:
            if orig in s.map:
                return s.map[orig]
            s = s.parent
        return None


class Renamer:
    def __init__(self, style="confusable", min_len=8):
        self.counter = 0
        self.style = style
        self.min_len = min_len

    def _fresh(self):
        n = self.counter
        self.counter += 1
        if self.style == "hex":
            return hex_name(n)
        return confusable_name(n, self.min_len)

    def declare(self, scope, orig):
        new = self._fresh()
        scope.map[orig] = new
        return new

    # -- entry -----------------------------------------------------------
    def run(self, chunk):
        root = Scope(None)
        self.visit_block(chunk.body, root)
        return chunk

    # -- blocks / statements ----------------------------------------------
    def visit_block(self, stmts, scope):
        for s in stmts:
            self.visit_stmt(s, scope)

    def visit_stmt(self, s, scope):
        k = s.kind
        if k == "LocalStmt":
            for e in s.exprs:
                self.visit_expr(e, scope)
            s.names = [self.declare(scope, nm) for nm in s.names]
            return
        if k == "AssignStmt":
            for t in s.targets:
                self.visit_expr(t, scope)
            for e in s.exprs:
                self.visit_expr(e, scope)
            return
        if k == "CallStmt":
            self.visit_expr(s.call, scope)
            return
        if k == "DoStmt":
            inner = Scope(scope)
            self.visit_block(s.body, inner)
            return
        if k == "WhileStmt":
            self.visit_expr(s.cond, scope)
            inner = Scope(scope)
            self.visit_block(s.body, inner)
            return
        if k == "RepeatStmt":
            inner = Scope(scope)
            self.visit_block(s.body, inner)
            # `until` condition can see locals declared in the repeat body
            self.visit_expr(s.cond, inner)
            return
        if k == "IfStmt":
            new_clauses = []
            for cond, body in s.clauses:
                self.visit_expr(cond, scope)
                inner = Scope(scope)
                self.visit_block(body, inner)
                new_clauses.append((cond, body))
            s.clauses = new_clauses
            if s.else_body is not None:
                inner = Scope(scope)
                self.visit_block(s.else_body, inner)
            return
        if k == "NumericForStmt":
            self.visit_expr(s.start, scope)
            self.visit_expr(s.stop, scope)
            if s.step is not None:
                self.visit_expr(s.step, scope)
            inner = Scope(scope)
            s.var = self.declare(inner, s.var)
            self.visit_block(s.body, inner)
            return
        if k == "GenericForStmt":
            for e in s.exprs:
                self.visit_expr(e, scope)
            inner = Scope(scope)
            s.names = [self.declare(inner, nm) for nm in s.names]
            self.visit_block(s.body, inner)
            return
        if k == "FunctionDeclStmt":
            # target name path: resolve (don't declare) the base identifier
            self.visit_funcname_target(s.target, scope)
            inner = Scope(scope)
            s.params = [self.declare(inner, p) for p in s.params]
            self.visit_block(s.body, inner)
            return
        if k == "LocalFunctionStmt":
            # visible to its own body (recursion) and after
            s.name = self.declare(scope, s.name)
            inner = Scope(scope)
            s.params = [self.declare(inner, p) for p in s.params]
            self.visit_block(s.body, inner)
            return
        if k == "ReturnStmt":
            for e in s.exprs:
                self.visit_expr(e, scope)
            return
        if k in ("BreakStmt", "ContinueStmt", "LabelStmt", "GotoStmt"):
            return
        raise ValueError(f"rename: unknown stmt kind {k}")

    def visit_funcname_target(self, target, scope):
        # Only the innermost base Name is a variable reference; `.foo` path
        # segments are field keys (Strings) and are never touched.
        node = target
        while node.kind == "Index":
            node = node.obj
        if node.kind == "Name":
            resolved = scope.resolve(node.name)
            if resolved is not None:
                node.name = resolved

    # -- expressions -------------------------------------------------------
    def visit_expr(self, e, scope):
        k = e.kind
        if k in ("Nil", "True", "False", "Vararg", "Number", "String"):
            return
        if k == "Name":
            resolved = scope.resolve(e.name)
            if resolved is not None:
                e.name = resolved
            return
        if k == "Paren":
            self.visit_expr(e.expr, scope)
            return
        if k == "Index":
            self.visit_expr(e.obj, scope)
            if not e.dot:
                self.visit_expr(e.key, scope)
            return
        if k == "Call":
            self.visit_expr(e.func, scope)
            for a in e.args:
                self.visit_expr(a, scope)
            return
        if k == "MethodCall":
            self.visit_expr(e.obj, scope)
            for a in e.args:
                self.visit_expr(a, scope)
            return
        if k == "FunctionExpr":
            inner = Scope(scope)
            e.params = [self.declare(inner, p) for p in e.params]
            self.visit_block(e.body, inner)
            return
        if k == "TableExpr":
            for f in e.fields:
                if f.ftype == "indexed":
                    self.visit_expr(f.key, scope)
                self.visit_expr(f.value, scope)
            return
        if k == "BinOp":
            self.visit_expr(e.left, scope)
            self.visit_expr(e.right, scope)
            return
        if k == "UnOp":
            self.visit_expr(e.operand, scope)
            return
        raise ValueError(f"rename: unknown expr kind {k}")


def rename(chunk, style="confusable", min_len=8):
    Renamer(style=style, min_len=min_len).run(chunk)
    return chunk
