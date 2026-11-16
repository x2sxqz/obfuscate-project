import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lua_obf.parser import parse
from lua_obf.codegen import CodeGen

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "sample1.lua")
src = open(path, encoding="utf-8").read()

ast = parse(src)
out = CodeGen(minify=False).gen_chunk(ast)
print("---- regenerated ----")
print(out)

# re-parse the regenerated output to make sure it's still valid
ast2 = parse(out)
out2 = CodeGen(minify=False).gen_chunk(ast2)
assert out == out2, "non-idempotent codegen!"
print("---- OK: re-parse + regen is idempotent ----")

out_min = CodeGen(minify=True).gen_chunk(ast)
ast3 = parse(out_min)
print("---- minified parses OK, length:", len(out_min), "----")
