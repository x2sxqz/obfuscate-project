"""Recursive-descent parser for Lua 5.1 (+ a few Luau niceties: `continue`,
`::label::`/`goto`, and compound assignment operators `+=` etc. which are
desugared at parse time)."""

from .lexer import tokenize
from .astnodes import N

BLOCK_END = {"end", "else", "elseif", "until", "EOF"}

BINOP_PRECEDENCE = {
    "or": (1, 1),
    "and": (2, 2),
    "<": (3, 3), ">": (3, 3), "<=": (3, 3), ">=": (3, 3), "~=": (3, 3), "==": (3, 3),
    "..": (9, 8),  # right assoc
    "+": (10, 10), "-": (10, 10),
    "*": (11, 11), "/": (11, 11), "%": (11, 11),
    "^": (14, 13),  # right assoc
}
UNARY_PRECEDENCE = 12
COMPOUND_OPS = {"+=": "+", "-=": "-", "*=": "*", "/=": "/", "%=": "%", "^=": "^", "..=": ".."}


class ParseError(Exception):
    pass


class Parser:
    def __init__(self, src):
        self.toks = tokenize(src)
        self.pos = 0

    # -- token helpers -----------------------------------------------
    def cur(self):
        return self.toks[self.pos]

    def at(self, type_):
        return self.cur().type == type_

    def at_any(self, types):
        return self.cur().type in types

    def advance(self):
        t = self.toks[self.pos]
        if self.pos < len(self.toks) - 1:
            self.pos += 1
        return t

    def expect(self, type_):
        if not self.at(type_):
            t = self.cur()
            raise ParseError(f"expected {type_!r} but got {t.type!r} ({t.value!r}) at line {t.line}")
        return self.advance()

    # -- entry ---------------------------------------------------------
    def parse_chunk(self):
        body = self.parse_block()
        self.expect("EOF")
        return N("Chunk", body=body)

    def parse_block(self):
        stmts = []
        while not self.at_any(BLOCK_END):
            if self.at("return"):
                stmts.append(self.parse_return())
                break
            s = self.parse_statement()
            if s is not None:
                stmts.append(s)
        return stmts

    # -- statements ------------------------------------------------------
    def parse_statement(self):
        t = self.cur()
        if t.type == ";":
            self.advance()
            return None
        if t.type == "if":
            return self.parse_if()
        if t.type == "while":
            return self.parse_while()
        if t.type == "do":
            self.advance()
            body = self.parse_block()
            self.expect("end")
            return N("DoStmt", body=body)
        if t.type == "for":
            return self.parse_for()
        if t.type == "repeat":
            return self.parse_repeat()
        if t.type == "function":
            return self.parse_function_stmt()
        if t.type == "local":
            return self.parse_local()
        if t.type == "break":
            self.advance()
            return N("BreakStmt")
        if t.type == "continue":
            self.advance()
            return N("ContinueStmt")
        if t.type == "::":
            self.advance()
            name = self.expect("NAME").value
            self.expect("::")
            return N("LabelStmt", name=name)
        if t.type == "goto":
            # 'goto' isn't in KEYWORDS set as reserved word by default lexer;
            # treat it as NAME == 'goto'
            pass
        if t.type == "NAME" and t.value == "goto":
            self.advance()
            name = self.expect("NAME").value
            return N("GotoStmt", name=name)
        return self.parse_expr_statement()

    def parse_return(self):
        self.expect("return")
        exprs = []
        if not self.at_any(BLOCK_END) and not self.at(";"):
            exprs = self.parse_exprlist()
        if self.at(";"):
            self.advance()
        return N("ReturnStmt", exprs=exprs)

    def parse_if(self):
        self.expect("if")
        clauses = []
        cond = self.parse_expr()
        self.expect("then")
        body = self.parse_block()
        clauses.append((cond, body))
        while self.at("elseif"):
            self.advance()
            c = self.parse_expr()
            self.expect("then")
            b = self.parse_block()
            clauses.append((c, b))
        else_body = None
        if self.at("else"):
            self.advance()
            else_body = self.parse_block()
        self.expect("end")
        return N("IfStmt", clauses=clauses, else_body=else_body)

    def parse_while(self):
        self.expect("while")
        cond = self.parse_expr()
        self.expect("do")
        body = self.parse_block()
        self.expect("end")
        return N("WhileStmt", cond=cond, body=body)

    def parse_repeat(self):
        self.expect("repeat")
        body = self.parse_block()
        self.expect("until")
        cond = self.parse_expr()
        return N("RepeatStmt", body=body, cond=cond)

    def parse_for(self):
        self.expect("for")
        name1 = self.expect("NAME").value
        if self.at("="):
            self.advance()
            start = self.parse_expr()
            self.expect(",")
            stop = self.parse_expr()
            step = None
            if self.at(","):
                self.advance()
                step = self.parse_expr()
            self.expect("do")
            body = self.parse_block()
            self.expect("end")
            return N("NumericForStmt", var=name1, start=start, stop=stop, step=step, body=body)
        else:
            names = [name1]
            while self.at(","):
                self.advance()
                names.append(self.expect("NAME").value)
            self.expect("in")
            exprs = self.parse_exprlist()
            self.expect("do")
            body = self.parse_block()
            self.expect("end")
            return N("GenericForStmt", names=names, exprs=exprs, body=body)

    def parse_function_stmt(self):
        self.expect("function")
        # funcname: Name {'.' Name} [':' Name]
        base = N("Name", name=self.expect("NAME").value)
        is_method = False
        while self.at(".") or self.at(":"):
            dot = self.advance()
            key = self.expect("NAME").value
            base = N("Index", obj=base, key=N("String", value=key), dot=True)
            if dot.type == ":":
                is_method = True
                break
        params, vararg, body = self.parse_funcbody()
        # NOTE: `self` is intentionally NOT added to params here. In real
        # Lua, `function t:m(...)` implicitly binds `self` without it being
        # part of the declared parameter list; our codegen relies on that
        # too (is_method alone adds the implicit self at call time via `:`).
        return N("FunctionDeclStmt", target=base, params=params, vararg=vararg, body=body, is_method=is_method)

    def parse_funcbody(self):
        self.expect("(")
        params = []
        vararg = False
        if not self.at(")"):
            while True:
                if self.at("..."):
                    self.advance()
                    vararg = True
                    break
                params.append(self.expect("NAME").value)
                if self.at(","):
                    self.advance()
                    continue
                break
        self.expect(")")
        body = self.parse_block()
        self.expect("end")
        return params, vararg, body

    def parse_local(self):
        self.expect("local")
        if self.at("function"):
            self.advance()
            name = self.expect("NAME").value
            params, vararg, body = self.parse_funcbody()
            return N("LocalFunctionStmt", name=name, params=params, vararg=vararg, body=body)
        names = [self.expect("NAME").value]
        attribs = [self._maybe_attrib()]
        while self.at(","):
            self.advance()
            names.append(self.expect("NAME").value)
            attribs.append(self._maybe_attrib())
        exprs = []
        if self.at("="):
            self.advance()
            exprs = self.parse_exprlist()
        return N("LocalStmt", names=names, attribs=attribs, exprs=exprs)

    def _maybe_attrib(self):
        # Luau attribute syntax: local x <const> = 1
        if self.at("<"):
            self.advance()
            a = self.expect("NAME").value
            self.expect(">")
            return a
        return None

    def parse_expr_statement(self):
        expr = self.parse_suffixedexpr()
        if self.at("=") or self.at(",") :
            targets = [expr]
            while self.at(","):
                self.advance()
                targets.append(self.parse_suffixedexpr())
            self.expect("=")
            exprs = self.parse_exprlist()
            return N("AssignStmt", targets=targets, exprs=exprs)
        if self.at_any(COMPOUND_OPS.keys()):
            op_tok = self.advance()
            rhs = self.parse_expr()
            op = COMPOUND_OPS[op_tok.type]
            new_val = N("BinOp", op=op, left=expr, right=rhs)
            return N("AssignStmt", targets=[expr], exprs=[new_val])
        if expr.kind not in ("Call", "MethodCall"):
            t = self.cur()
            raise ParseError(f"syntax error near line {t.line} (unexpected expression statement)")
        return N("CallStmt", call=expr)

    # -- expressions -----------------------------------------------------
    def parse_exprlist(self):
        exprs = [self.parse_expr()]
        while self.at(","):
            self.advance()
            exprs.append(self.parse_expr())
        return exprs

    def parse_expr(self, min_prec=0):
        if self.at_any(("not", "-", "#")):
            op_tok = self.advance()
            operand = self.parse_expr(UNARY_PRECEDENCE)
            left = N("UnOp", op=op_tok.type, operand=operand)
        else:
            left = self.parse_simpleexpr()
        while True:
            t = self.cur().type
            if t not in BINOP_PRECEDENCE:
                break
            lp, rp = BINOP_PRECEDENCE[t]
            if lp < min_prec:
                break
            self.advance()
            right = self.parse_expr(rp + 1 if t not in ("..", "^") else rp)
            left = N("BinOp", op=t, left=left, right=right)
        return left

    def parse_simpleexpr(self):
        t = self.cur()
        if t.type == "NUMBER":
            self.advance()
            return N("Number", raw=t.value)
        if t.type == "STRING":
            self.advance()
            return N("String", value=t.value)
        if t.type == "nil":
            self.advance()
            return N("Nil")
        if t.type == "true":
            self.advance()
            return N("True")
        if t.type == "false":
            self.advance()
            return N("False")
        if t.type == "...":
            self.advance()
            return N("Vararg")
        if t.type == "function":
            self.advance()
            params, vararg, body = self.parse_funcbody()
            return N("FunctionExpr", params=params, vararg=vararg, body=body)
        if t.type == "{":
            return self.parse_table()
        return self.parse_suffixedexpr()

    def parse_primaryexpr(self):
        t = self.cur()
        if t.type == "(":
            self.advance()
            e = self.parse_expr()
            self.expect(")")
            return N("Paren", expr=e)
        if t.type == "NAME":
            self.advance()
            return N("Name", name=t.value)
        raise ParseError(f"unexpected token {t.type!r} ({t.value!r}) at line {t.line}")

    def parse_suffixedexpr(self):
        expr = self.parse_primaryexpr()
        while True:
            t = self.cur()
            if t.type == ".":
                self.advance()
                key = self.expect("NAME").value
                expr = N("Index", obj=expr, key=N("String", value=key), dot=True)
            elif t.type == "[":
                self.advance()
                key = self.parse_expr()
                self.expect("]")
                expr = N("Index", obj=expr, key=key, dot=False)
            elif t.type == ":":
                self.advance()
                method = self.expect("NAME").value
                args = self.parse_args()
                expr = N("MethodCall", obj=expr, method=method, args=args)
            elif t.type in ("(", "STRING", "{"):
                args = self.parse_args()
                expr = N("Call", func=expr, args=args)
            else:
                break
        return expr

    def parse_args(self):
        t = self.cur()
        if t.type == "(":
            self.advance()
            args = []
            if not self.at(")"):
                args = self.parse_exprlist()
            self.expect(")")
            return args
        if t.type == "STRING":
            self.advance()
            return [N("String", value=t.value)]
        if t.type == "{":
            return [self.parse_table()]
        raise ParseError(f"expected call arguments at line {t.line}")

    def parse_table(self):
        self.expect("{")
        fields = []
        while not self.at("}"):
            if self.at("["):
                self.advance()
                key = self.parse_expr()
                self.expect("]")
                self.expect("=")
                val = self.parse_expr()
                fields.append(N("TField", ftype="indexed", key=key, value=val))
            elif self.at("NAME") and self.toks[self.pos + 1].type == "=":
                name = self.advance().value
                self.advance()  # '='
                val = self.parse_expr()
                fields.append(N("TField", ftype="named", key=name, value=val))
            else:
                val = self.parse_expr()
                fields.append(N("TField", ftype="pos", key=None, value=val))
            if self.at(",") or self.at(";"):
                self.advance()
                continue
            break
        self.expect("}")
        return N("TableExpr", fields=fields)


def parse(src):
    return Parser(src).parse_chunk()
