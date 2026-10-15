-- omadown - Hyprland bindings and window rules for Omarchy.
--
-- LEGACY WARNING: this file used to be `omadown.conf`, a plain Hyprland config
-- dropped into ~/.config/omarchy/extensions/. That NEVER WORKED on current
-- Omarchy, which has migrated its Hyprland config to Lua: nothing sources
-- `extensions/*.conf` (that directory is for menu .jsonc files and .sh hooks
-- only), so every binding defined there was silently dead and Super+D did
-- nothing. Do not reintroduce a .conf file.
--
-- Install now installs this Lua module. It is loaded one of two ways, whichever
-- the host config supports:
--   1. `require("hypr.omadown")` from ~/.config/hypr/hyprland.lua  (preferred)
--   2. `dofile(.../omadown.lua)` from the same place               (fallback)
--
-- Both work because this file only touches globals (`o`, `hl`) and returns a
-- table; it does not depend on `require` semantics. The install script appends
-- the require/dofile line itself, guarded so it is idempotent.

local M = {}

-- Super + D          queue UI
-- Super + Shift + D  download whatever is in the clipboard
-- Super + Alt + D    queue summary as a notification
--
-- NOTE: SUPER + D is deliberately NOT bound via `{ tui = "omadown tui" }`.
-- That form expands to `omarchy-launch-tui 'omadown tui'`, i.e. a single argv
-- entry containing a space, and the terminal then tries to exec a binary
-- literally named "omadown tui" and fails with ENOENT. The omadown-tui wrapper
-- splits the argv itself, so call it by name.

-- omadown-tui is only usable when the install actually put it on PATH; fall
-- back to the CLI so the keybinding does something rather than nothing.
local function omadown_on_path(name)
  if o and o.cmd_present then
    return o.cmd_present(name)
  end
  local path = os.getenv("PATH") or ""
  for dir in (path .. ":"):gmatch("([^:]*):") do
    local handle = io.open((dir ~= "" and dir or ".") .. "/" .. name, "r")
    if handle then
      handle:close()
      return true
    end
  end
  return false
end

local function bind(keys, description, dispatcher)
  if o and o.bind then
    o.bind(keys, description, dispatcher)
  elseif hl and hl.bind then
    hl.bind(keys, dispatcher, { description = description })
  end
end

if omadown_on_path("omadown-tui") then
  bind("SUPER + D", "omadown queue", "omadown-tui")
elseif omadown_on_path("omadown") then
  bind("SUPER + D", "omadown queue", "omadown status")
end

bind("SUPER + SHIFT + D", "omadown: download clipboard link", "omadown-clipboard-add")
bind("SUPER + ALT + D", "omadown: status", "omadown-notify-status")

-- Float the queue UI. omadown-tui launches through omarchy-launch-tui, whose
-- app-id defaults to `org.omarchy.<basename>` = org.omarchy.omadown. The window
-- keeps the terminal's own title (foot reports its title as `foot`), so match on
-- class only.
--
-- o.window(match, rules): the FIRST argument is a match table and the SECOND is
-- the rules table. Swapping them (or putting rules in the match table) makes the
-- helper build a nonsense nested match and the rule is silently discarded --
-- which is why this file spells them out explicitly.
if o and o.window then
  o.window({ class = "^omadown$" }, { float = true, size = { 1000, 640 }, center = true })
  o.window(
    { class = "^org%.omarchy%.omadown$" },
    { float = true, size = { 1000, 640 }, center = true }
  )
end

return M
