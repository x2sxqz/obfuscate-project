-- sample script
local function greet(name)
    local msg = "Hello, " .. name .. "!"
    print(msg)
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
        print(self.name .. " died")
    end
end

local p = Players.new("Hero")
for i = 1, 5 do
    p:takeDamage(10 * i)
    if p.hp == 0 then
        break
    end
end

local t = {1, 2, 3, x = 10, [100] = "far"}
local sum = 0
for i, v in ipairs(t) do
    sum = sum + v
end

local ok = (sum > 0) and "yes" or "no"
greet("World")

local function vartest(...)
    local a, b = ...
    return a, b
end

local nested = function()
    return function()
        return 42
    end
end

print(nested()())
