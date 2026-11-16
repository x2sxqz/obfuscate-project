"""AST -> Lua source code generator."""

LUA_KEYWORDS = {
    "and", "break", "do", "else", "elseif", "end", "false", "for",
    "function", "if", "in", "local", "nil", "not", "or", "repeat",
    "return", "then", "true", "until", "while",
}


def lua_string_literal(s):
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif 32 <= o < 127:
            out.append(ch)
        else:
            out.append(f"\\{o}")
    out.append('"')
    return "".join(out)


class CodeGen:
    def __init__(self, minify=False, indent_str="  "):
        self.minify = minify
        self.indent_str = indent_str
        self._depth = 0

    def gen_chunk(self, chunk):
        return self.gen_block(chunk.body, 0)

    # -- blocks -----------------------------------------------------------
    def gen_block(self, stmts, depth):
        lines = [self.gen_stmt(s, depth) for s in stmts]
        lines = [l for l in lines if l is not None]
        # A semicolon is always a safe statement separator in Lua/Luau, even
        # right after a block-ending `end`/`break`/etc. Without it, minified
        # output like `end` immediately followed by `local` becomes the
        # single identifier `endlocal`.
        sep = ";" if self.minify else "\n"
        return sep.join(lines)

    def ind(self, depth):
        return "" if self.minify else self.indent_str * depth

    @staticmethod
    def format_params(params, vararg):
        parts = list(params)
        if vararg:
            parts.append("...")
        return ",".join(parts)

    # -- statements ---------------------------------------------------
    def gen_stmt(self, s, depth):
        k = s.kind
        pre = self.ind(depth)
        self._depth = depth
        if k == "LocalStmt":
            names = []
            for nm, at in zip(s.names, s.attribs):
                names.append(nm + (f"<{at}>" if at else ""))
            left = "local " + ",".join(names)
            if s.exprs:
                left += "=" + ",".join(self.gen_expr(e) for e in s.exprs)
            return pre + left
        if k == "AssignStmt":
            left = ",".join(self.gen_expr(t) for t in s.targets)
            right = ",".join(self.gen_expr(e) for e in s.exprs)
            return pre + left + "=" + right
        if k == "CallStmt":
            return pre + self.gen_expr(s.call)
        if k == "DoStmt":
            inner = self.gen_block(s.body, depth + 1)
            nl = " " if self.minify else "\n"
            return f"{pre}do{nl}{inner}{nl}{pre}end"
        if k == "WhileStmt":
            inner = self.gen_block(s.body, depth + 1)
            nl = " " if self.minify else "\n"
            return f"{pre}while {self.gen_expr(s.cond)} do{nl}{inner}{nl}{pre}end"
        if k == "RepeatStmt":
            inner = self.gen_block(s.body, depth + 1)
            nl = " " if self.minify else "\n"
            return f"{pre}repeat{nl}{inner}{nl}{pre}until {self.gen_expr(s.cond)}"
        if k == "IfStmt":
            nl = " " if self.minify else "\n"
            parts = []
            first = True
            for cond, body in s.clauses:
                kw = "if" if first else "elseif"
                first = False
                parts.append(f"{pre}{kw} {self.gen_expr(cond)} then{nl}{self.gen_block(body, depth+1)}")
            if s.else_body is not None:
                parts.append(f"{pre}else{nl}{self.gen_block(s.else_body, depth+1)}")
            return nl.join(parts) + nl + pre + "end"
        if k == "NumericForStmt":
            inner = self.gen_block(s.body, depth + 1)
            nl = " " if self.minify else "\n"
            step = "," + self.gen_expr(s.step) if s.step is not None else ""
            return (f"{pre}for {s.var}={self.gen_expr(s.start)},{self.gen_expr(s.stop)}{step} do"
                    f"{nl}{inner}{nl}{pre}end")
        if k == "GenericForStmt":
            inner = self.gen_block(s.body, depth + 1)
            nl = " " if self.minify else "\n"
            names = ",".join(s.names)
            exprs = ",".join(self.gen_expr(e) for e in s.exprs)
            return f"{pre}for {names} in {exprs} do{nl}{inner}{nl}{pre}end"
        if k == "FunctionDeclStmt":
            inner = self.gen_block(s.body, depth + 1)
            nl = " " if self.minify else "\n"
            plist = self.format_params(s.params, s.vararg)
            target_src = self.gen_funcname(s.target, s.is_method)
            return f"{pre}function {target_src}({plist}){nl}{inner}{nl}{pre}end"
        if k == "LocalFunctionStmt":
            inner = self.gen_block(s.body, depth + 1)
            nl = " " if self.minify else "\n"
            plist = self.format_params(s.params, s.vararg)
            return f"{pre}local function {s.name}({plist}){nl}{inner}{nl}{pre}end"
        if k == "ReturnStmt":
            if s.exprs:
                return pre + "return " + ",".join(self.gen_expr(e) for e in s.exprs)
            return pre + "return"
        if k == "BreakStmt":
            return pre + "break"
        if k == "ContinueStmt":
            return pre + "continue"
        if k == "LabelStmt":
            return pre + f"::{s.name}::"
        if k == "GotoStmt":
            return pre + f"goto {s.name}"
        raise ValueError(f"unknown stmt kind {k}")

    def gen_funcname(self, target, is_method):
        parts = []
        node = target
        while node.kind == "Index":
            parts.append(node.key.value)
            node = node.obj
        assert node.kind == "Name"
        parts.append(node.name)
        parts.reverse()
        if is_method:
            return ".".join(parts[:-1]) + ":" + parts[-1]
        return ".".join(parts)

    # -- expressions ----------------------------------------------------
    def gen_expr(self, e):
        k = e.kind
        if k == "Nil":
            return "nil"
        if k == "True":
            return "true"
        if k == "False":
            return "false"
        if k == "Vararg":
            return "..."
        if k == "Number":
            return e.raw
        if k == "String":
            return lua_string_literal(e.value)
        if k == "Name":
            return e.name
        if k == "Paren":
            return "(" + self.gen_expr(e.expr) + ")"
        if k == "Index":
            if e.dot:
                return self.gen_expr(e.obj) + "." + e.key.value
            return self.gen_expr(e.obj) + "[" + self.gen_expr(e.key) + "]"
        if k == "Call":
            return self.gen_expr(e.func) + "(" + ",".join(self.gen_expr(a) for a in e.args) + ")"
        if k == "MethodCall":
            return self.gen_expr(e.obj) + ":" + e.method + "(" + ",".join(self.gen_expr(a) for a in e.args) + ")"
        if k == "FunctionExpr":
            outer_depth = self._depth
            inner = self.gen_block(e.body, outer_depth + 1)
            self._depth = outer_depth
            nl = " " if self.minify else "\n"
            plist = self.format_params(e.params, e.vararg)
            return f"function({plist}){nl}{inner}{nl}{self.ind(outer_depth)}end"
        if k == "TableExpr":
            parts = []
            for f in e.fields:
                if f.ftype == "pos":
                    parts.append(self.gen_expr(f.value))
                elif f.ftype == "named":
                    parts.append(f"{f.key}={self.gen_expr(f.value)}")
                else:
                    parts.append(f"[{self.gen_expr(f.key)}]={self.gen_expr(f.value)}")
            return "{" + ",".join(parts) + "}"
        if k == "BinOp":
            # Always keep a space around operators: with minify on, things
            # like `a- -1` -> `a--1` would start a comment, and `1..2`
            # would be misread as part of a number literal, so correctness
            # wins over the extra byte here.
            return self.gen_expr(e.left) + " " + e.op + " " + self.gen_expr(e.right)
        if k == "UnOp":
            return e.op + " " + self.gen_expr(e.operand)
        raise ValueError(f"unknown expr kind {k}")
