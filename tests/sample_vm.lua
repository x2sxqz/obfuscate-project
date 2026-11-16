-- pure, closed functions: good VM candidates
local function add(a, b)
    return a + b
end

local function fact(n)
    local result = 1
    local i = 1
    while i <= n do
        result = result * i
        i = i + 1
    end
    return result
end

local function fib(n)
    if n <= 1 then
        return n
    end
    local a = 0
    local b = 1
    local i = 2
    while i <= n do
        local c = a + b
        a = b
        b = c
        i = i + 1
    end
    return b
end

local function sumRange(lo, hi)
    local total = 0
    for i = lo, hi do
        total = total + i
    end
    return total
end

local function isEven(n)
    return n % 2 == 0
end

local function classify(n)
    if n < 0 then
        return "negative"
    elseif n == 0 then
        return "zero"
    else
        return "positive"
    end
end

local function checksum(s)
    local sum = 0
    for i = 1, #s do
        sum = sum + string.byte(s, i)
    end
    return sum % 256
end

local function makeList(n)
    local t = {}
    for i = 1, n do
        t[i] = i * i
    end
    return table.concat(t, ",")
end

local function loopWithBreak(limit)
    local count = 0
    for i = 1, 1000 do
        if i > limit then
            break
        end
        count = count + 1
    end
    return count
end

local function repeatTest()
    local n = 0
    repeat
        n = n + 1
    until n >= 5
    return n
end

print(add(3, 4))
print(fact(5))
print(fib(10))
print(sumRange(1, 100))
print(isEven(4), isEven(7))
print(classify(-1), classify(0), classify(5))
print(checksum("hello"))
print(makeList(5))
print(loopWithBreak(7))
print(repeatTest())

-- this one captures an upvalue by READING it (should still vmify)
local BONUS = 10
local function addBonus(x)
    return x + BONUS
end
print(addBonus(5))

-- this one is NOT eligible (nested function / closure) -- must still run correctly, just unvirtualized
local function makeAdder(n)
    return function(x) return x + n end
end
local add5 = makeAdder(5)
print(add5(100))
