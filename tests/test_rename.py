import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lua_obf.parser import parse
from lua_obf.codegen import CodeGen
from lua_obf.transforms.rename import rename

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "sample1.lua")
src = open(path, encoding="utf-8").read()

ast = parse(src)
rename(ast, style="hex")
out = CodeGen(minify=False).gen_chunk(ast)
print(out)

# must still re-parse
ast2 = parse(out)
print("---- OK: renamed code re-parses ----")
