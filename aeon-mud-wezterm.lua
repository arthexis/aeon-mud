-- Aeon MUD's dedicated WezTerm shortcut layer.
-- Loaded only by aeon-mud-wezterm.ps1: do not alter the user's global config.
local wezterm = require 'wezterm'
local act = wezterm.action
local config = wezterm.config_builder()

config.keys = {
  -- Ctrl+C copies the terminal selection. Never forward ETX/SIGINT to the MUD.
  { key = 'c', mods = 'CTRL', action = act.CopyTo 'Clipboard' },
  -- Send Ctrl+V to the Aeon input handler rather than injecting raw clipboard
  -- data: Aeon confirms multiline pastes before sending any game commands.
  { key = 'v', mods = 'CTRL', action = act.SendString '\\x16' },
  { key = 'v', mods = 'CTRL|SHIFT', action = act.SendString '\\x16' },
  { key = 'Insert', mods = 'SHIFT', action = act.SendString '\\x16' },
}

return config
