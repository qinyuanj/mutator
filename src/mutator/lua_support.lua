-- Bootstrap for mutator's generated Busted command (Lua 5.4), dofile'd by the
-- -e preamble before busted loads. paths() pins the module search path to the
-- LuaRocks trees the plan computed; install() caches compiled chunks and swaps
-- busted's per-element debug.traceback for fasttrace (the C-side global-name
-- lookup dominates suite loading on large projects). Escape hatches:
-- MUTATOR_LUA_NO_CACHE=1, MUTATOR_LUA_NO_FASTTRACE=1.
return function(support_dir)
  local function apply_cache()
    if os.getenv("MUTATOR_LUA_NO_CACHE") == "1" then return end
    local real_load, real_loadfile, real_dofile = load, loadfile, dofile
    local dump, open = string.dump, io.open
    local cache = {}
    local counters = { hits = 0, misses = 0 }
    _G.MUTATOR_LUA_CACHE = counters
    local function cached_load(text, name, mode, env)
      -- An explicit env means the caller owns the chunk's globals; never share.
      if type(text) == "string" and env == nil then
        local key = (name or "") .. "\0" .. text
        local hit = cache[key]
        if hit then
          counters.hits = counters.hits + 1
          return real_load(hit, name, "b")
        end
        local chunk, err = real_load(text, name, mode)
        if chunk then
          counters.misses = counters.misses + 1
          cache[key] = dump(chunk)
        end
        return chunk, err
      end
      return real_load(text, name, mode, env)
    end
    local function cached_loadfile(path, mode, env)
      if path == nil then return real_loadfile(nil, mode, env) end
      local file = open(path, "rb")
      if file == nil then return real_loadfile(path, mode, env) end
      local text = file:read("a")
      file:close()
      -- loadfile strips a shebang but keeps its newline, so line numbers hold.
      return cached_load((text:gsub("^#[^\n]*", "", 1)), "@" .. path, mode, env)
    end
    _G.load = cached_load
    _G.loadfile = cached_loadfile
    _G.dofile = function(path)
      if path == nil then return real_dofile() end
      local chunk, err = cached_loadfile(path)
      if chunk == nil then error(err, 0) end
      return chunk()
    end
  end

  local function apply_fasttrace()
    if os.getenv("MUTATOR_LUA_NO_FASTTRACE") == "1" then return end
    if _VERSION ~= "Lua 5.4" then return end
    local ok, busted = pcall(require, "busted")
    if not ok or type(busted) ~= "table" or busted.version ~= "2.3.0" then return end
    if type(busted.getTrace) ~= "function" or type(busted.getFile) ~= "function" then return end
    local origin = debug.getinfo(busted.getTrace, "S")
    if origin == nil or origin.what ~= "Lua" then return end
    if type(origin.source) ~= "string" or origin.source:sub(1, 1) ~= "@" then return end
    local dirname = require("pl.path").dirname
    local thisdir = dirname(origin.source)
    local fasttrace = dofile(support_dir .. "/fasttrace.lua")
    local getinfo = debug.getinfo
    -- Same body as busted 2.3.0 core.lua getTrace, except the traceback only
    -- formats the frames trimTrace keeps (adr: fasttrace verbose text).
    function busted.getTrace(element, level, msg)
      local function trimTrace(info)
        local index = info.traceback:find("\n%s*%[C]")
        info.traceback = info.traceback:sub(1, index)
        return info
      end
      level = level or 3
      local info = getinfo(level, "Sl")
      while info.what == "C" or info.short_src:match("luassert[/\\].*%.lua$") or
            (info.source:sub(1, 1) == "@" and thisdir == dirname(info.source)) do
        level = level + 1
        info = getinfo(level, "Sl")
      end
      info.traceback = fasttrace.trimmed(level, false) or debug.traceback("", level)
      info.message = tostring(msg)
      local file = busted.getFile(element)
      return file and file.getTrace(file.name, info) or trimTrace(info)
    end
  end

  return {
    install = function()
      apply_cache()
      apply_fasttrace()
    end,
  }
end
