-- tricky multi-value edge cases
local function f() return 1, 2, 3 end
local function g(...) return ... end
local function h(a, b, ...) return a + b, ... end

-- call in middle (truncated to 1) vs tail (expanded)
print(f(), 99)        -- 1  99   (f truncated to 1 in non-tail)
print(99, f())        -- 99 1 2 3 (f expanded in tail)
print((f()))          -- 1        (parens truncate)

-- vararg forwarding and mixing
print(g(10, 20, 30))  -- 10 20 30
print(h(1, 2, 3, 4))  -- 3 3 4

-- nested calls
print(g(f()))         -- 1 2 3
print(g(f(), f()))    -- 1 1 2 3

-- table with multi tail vs middle
local t1 = {f()}
print(#t1, t1[1], t1[3])   -- 3 1 3
local t2 = {f(), 0}
print(#t2, t2[1], t2[2])   -- 2 1 0

-- multi-assign fewer/more
local a, b = f()
print(a, b)                -- 1 2
local c, d, e, x = g(7, 8)
print(c, d, e, x)          -- 7 8 nil nil
