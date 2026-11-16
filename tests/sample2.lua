local function greet(name)
    local msg = "Hello, " .. name .. "!"
    return msg
end

local Players = {}
Players.__index = Players

function Players.new(name)
    local self = setmetatable({}, Players)
    self.name = name
    self.hp = 100
    return self
end

function Players:takeDamage(amount)
    self.hp = self.hp - amount
    if self.hp <= 0 then
        self.hp = 0
    end
end

local p = Players.new("Hero")
for i = 1, 5 do
    p:takeDamage(10 * i)
    if p.hp == 0 then
        break
    end
end
print(p.name, p.hp)

local t = {1, 2, 3, x = 10, [100] = "far"}
local sum = 0
for i, v in ipairs(t) do
    sum = sum + v
end
print("sum", sum, t.x, t[100])

local ok = (sum > 0) and "yes" or "no"
print(ok)
print(greet("World"))

local function fact(n)
    if n <= 1 then
        return 1
    end
    return n * fact(n - 1)
end
print("fact5", fact(5))

local function vartest(...)
    local a, b, c = ...
    return a, b, c
end
print(vartest(10, 20, 30))

local nested = function()
    return function()
        return 42
    end
end
print(nested()())

local acc = 0
local n = 0
while n < 10 do
    n = n + 1
    if n % 2 == 0 then
        acc = acc + n
    end
end
print("acc", acc)

local r = 0
repeat
    r = r + 1
until r >= 3
print("r", r)

local counted = 0
for i = 1, 10 do
    if i > 5 then
        break
    end
    counted = counted + 1
end
print("counted", counted)

local words = {"alpha", "beta", "gamma"}
local joined = table.concat(words, ",")
print("joined", joined)

local function makeCounter()
    local count = 0
    return function()
        count = count + 1
        return count
    end
end
local c1 = makeCounter()
print(c1(), c1(), c1())
