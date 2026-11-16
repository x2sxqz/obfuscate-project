-- recursive local function
local function fib(n)
  if n < 2 then return n end
  return fib(n-1) + fib(n-2)
end
print(fib(10))                         -- 55

-- closure returning multiple values + capturing
local function stats(t)
  local total = 0
  local function add(x) total = total + x end
  for _, v in ipairs(t) do add(v) end
  return total, total / #t
end
print(stats({2, 4, 6, 8}))             -- 20  5

-- closure with varargs capturing an upvalue
local function logger(prefix)
  return function(...)
    local parts = {prefix}
    for _, v in ipairs({...}) do parts[#parts+1] = tostring(v) end
    return table.concat(parts, ":")
  end
end
local log = logger("LOG")
print(log("a", "b", "c"))              -- LOG:a:b:c

-- mutual-ish: counter factory with reset closure sharing cell
local function makeAcc()
  local sum = 0
  local function acc(x) sum = sum + x return sum end
  local function reset() sum = 0 end
  return acc, reset
end
local acc, reset = makeAcc()
print(acc(10), acc(5))                 -- 10 15
reset()
print(acc(3))                          -- 3

-- OOP: method building a closure capturing self
local Obj = {}
Obj.__index = Obj
function Obj.new(v) return setmetatable({v = v}, Obj) end
function Obj:getter()
  return function() return self.v end
end
local o = Obj.new(99)
local g = o:getter()
o.v = 123
print(g())                             -- 123 (captures self, sees mutation)

-- deeply nested loops + closures in table
local function grid(n)
  local cells = {}
  for i = 1, n do
    for j = 1, n do
      cells[#cells+1] = function() return i * 10 + j end
    end
  end
  return cells
end
local cs = grid(2)
print(cs[1](), cs[2](), cs[3](), cs[4]())  -- 11 12 21 22
