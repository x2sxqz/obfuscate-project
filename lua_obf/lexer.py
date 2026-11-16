"""Lua 5.1 / Luau-subset lexer."""

KEYWORDS = {
    "and", "break", "do", "else", "elseif", "end", "false", "for",
    "function", "if", "in", "local", "nil", "not", "or", "repeat",
    "return", "then", "true", "until", "while",
    # Luau extras we tolerate as statements (continue is just an ident-like
    # keyword used as a statement keyword in Luau)
    "continue",
}

# Multi-char operators, longest first
SYMBOLS = [
    "...", "..", ".", "::", ":", "==", "~=", "<=", ">=", "<", ">", "=",
    "(", ")", "{", "}", "[", "]", ";", ",", "+", "-", "*", "/", "%",
    "^", "#", "..", "..=", "+=", "-=", "*=", "/=", "%=", "^=",
]
# sort by length desc for correct longest-match
SYMBOLS = sorted(set(SYMBOLS), key=len, reverse=True)


class Token:
    __slots__ = ("type", "value", "line", "col")

    def __init__(self, type_, value, line, col):
        self.type = type_
        self.value = value
        self.line = line
        self.col = col

    def __repr__(self):
        return f"Token({self.type!r},{self.value!r},L{self.line})"


class LexError(Exception):
    pass


class Lexer:
    def __init__(self, src):
        self.src = src
        self.n = len(src)
        self.i = 0
        self.line = 1
        self.col = 1

    def _peek(self, off=0):
        j = self.i + off
        return self.src[j] if j < self.n else ""

    def _advance(self):
        c = self.src[self.i]
        self.i += 1
        if c == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return c

    def _match_long_bracket_level(self):
        """At a '[', check for [=*[ opener; return level or None, consuming it if found."""
        save = self.i
        if self._peek() != "[":
            return None
        j = self.i + 1
        level = 0
        while j < self.n and self.src[j] == "=":
            level += 1
            j += 1
        if j < self.n and self.src[j] == "[":
            # consume
            while self.i <= j:
                self._advance()
            return level
        self.i = save
        return None

    def _read_long_bracket_body(self, level):
        # skip first newline immediately after opener
        if self._peek() == "\r":
            self._advance()
            if self._peek() == "\n":
                self._advance()
        elif self._peek() == "\n":
            self._advance()
        start = self.i
        closer = "]" + ("=" * level) + "]"
        idx = self.src.find(closer, self.i)
        if idx == -1:
            raise LexError(f"unterminated long bracket at line {self.line}")
        text = self.src[start:idx]
        # advance position/line tracking
        while self.i < idx + len(closer):
            self._advance()
        return text

    def tokenize(self):
        tokens = []
        while True:
            self._skip_ws_and_comments()
            if self.i >= self.n:
                tokens.append(Token("EOF", None, self.line, self.col))
                break
            c = self._peek()
            line, col = self.line, self.col
            if c.isalpha() or c == "_":
                tokens.append(self._read_name(line, col))
            elif c.isdigit() or (c == "." and self._peek(1).isdigit()):
                tokens.append(self._read_number(line, col))
            elif c == '"' or c == "'":
                tokens.append(self._read_short_string(line, col))
            elif c == "[" and (self._peek(1) == "[" or self._peek(1) == "="):
                level = self._match_long_bracket_level()
                if level is not None:
                    body = self._read_long_bracket_body(level)
                    tokens.append(Token("STRING", body, line, col))
                else:
                    self._advance()
                    tokens.append(Token("[", "[", line, col))
            else:
                tokens.append(self._read_symbol(line, col))
        return tokens

    def _skip_ws_and_comments(self):
        while self.i < self.n:
            c = self._peek()
            if c in " \t\r\n":
                self._advance()
                continue
            if c == "-" and self._peek(1) == "-":
                self._advance()
                self._advance()
                level = self._match_long_bracket_level()
                if level is not None:
                    self._read_long_bracket_body(level)
                else:
                    while self.i < self.n and self._peek() != "\n":
                        self._advance()
                continue
            break

    def _read_name(self, line, col):
        start = self.i
        while self.i < self.n and (self._peek().isalnum() or self._peek() == "_"):
            self._advance()
        text = self.src[start:self.i]
        if text in KEYWORDS:
            return Token(text, text, line, col)
        return Token("NAME", text, line, col)

    def _read_number(self, line, col):
        start = self.i
        if self._peek() == "0" and self._peek(1) in ("x", "X"):
            self._advance()
            self._advance()
            while self.i < self.n and (self._peek() in "0123456789abcdefABCDEF"):
                self._advance()
        else:
            while self.i < self.n and self._peek().isdigit():
                self._advance()
            if self._peek() == ".":
                self._advance()
                while self.i < self.n and self._peek().isdigit():
                    self._advance()
            if self._peek() in ("e", "E"):
                self._advance()
                if self._peek() in ("+", "-"):
                    self._advance()
                while self.i < self.n and self._peek().isdigit():
                    self._advance()
        text = self.src[start:self.i]
        return Token("NUMBER", text, line, col)

    def _read_short_string(self, line, col):
        quote = self._advance()
        out = []
        while True:
            if self.i >= self.n:
                raise LexError(f"unterminated string at line {line}")
            c = self._peek()
            if c == quote:
                self._advance()
                break
            if c == "\n":
                raise LexError(f"unterminated string at line {line}")
            if c == "\\":
                self._advance()
                e = self._peek()
                simple = {
                    "n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b",
                    "f": "\f", "v": "\v", "\\": "\\", '"': '"', "'": "'",
                    "\n": "\n",
                }
                if e in simple:
                    self._advance()
                    out.append(simple[e])
                elif e.isdigit():
                    digs = ""
                    for _ in range(3):
                        if self._peek().isdigit():
                            digs += self._advance()
                        else:
                            break
                    out.append(chr(int(digs)))
                elif e == "x":
                    self._advance()
                    digs = ""
                    for _ in range(2):
                        if self._peek() in "0123456789abcdefABCDEF":
                            digs += self._advance()
                    out.append(chr(int(digs, 16)))
                elif e == "z":
                    self._advance()
                    while self._peek() in " \t\r\n":
                        self._advance()
                else:
                    out.append(self._advance())
            else:
                out.append(self._advance())
        return Token("STRING", "".join(out), line, col)

    def _read_symbol(self, line, col):
        for sym in SYMBOLS:
            if self.src.startswith(sym, self.i):
                for _ in sym:
                    self._advance()
                return Token(sym, sym, line, col)
        bad = self._advance()
        raise LexError(f"unexpected character {bad!r} at line {line}")


def tokenize(src):
    return Lexer(src).tokenize()
