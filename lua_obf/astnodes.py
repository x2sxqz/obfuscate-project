"""Lightweight AST node definitions. Each node is a simple object with a
`kind` string plus whatever fields are relevant; this keeps the parser and
transform passes easy to write without a big class hierarchy."""


class Node:
    def __init__(self, kind, **fields):
        self.kind = kind
        self.__dict__.update(fields)

    def __repr__(self):
        items = {k: v for k, v in self.__dict__.items() if k != "kind"}
        return f"<{self.kind} {items}>"


def N(kind, **fields):
    return Node(kind, **fields)
