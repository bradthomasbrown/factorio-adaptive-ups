-- Wall-clock measurements stay outside the deterministic simulation. Commands
-- received from the server are replayed identically by every peer.
local function state()
  storage.adaptive_ups = storage.adaptive_ups or {
    enabled = false, floor = 15, last_pulse = game.tick
  }
  return storage.adaptive_ups
end

local function reply(command, value)
  local text = helpers.table_to_json(value)
  if command.player_index then
    game.get_player(command.player_index).print(text)
  else
    rcon.print(text)
  end
end

local function authorized(command)
  if not command.player_index then return true end
  local player = game.get_player(command.player_index)
  if player and player.admin then return true end
  reply(command, {error = "Administrator required"})
  return false
end

local function parse_ups(value)
  local ups = tonumber(value)
  if not ups or ups ~= ups or ups < 0.6 or ups > 60 then return nil end
  return ups
end

local function current_status()
  local s, names = state(), {}
  for _, player in pairs(game.connected_players) do names[#names + 1] = player.name end
  table.sort(names)
  return {tick = game.tick, players = names, target_ups = game.speed * 60,
          enabled = s.enabled, owner = s.owner, fallback_ups = s.floor}
end

local function write_roster()
  local status = current_status()
  -- Server-side notification lets an empty server stay paused without RCON
  -- queries, which would otherwise force simulation ticks while paused.
  helpers.write_file("adaptive-ups/roster.json", helpers.table_to_json(status), false, 0)
end

local function owner_matches(command, session)
  local s = state()
  if s.owner == session then return true end
  reply(command, {error = "Another controller owns adaptive mode", code = "owner_mismatch"})
  return false
end

commands.add_command("adaptive-ups-status", "Read status without changing watchdog or markers", function(command)
  if not authorized(command) then return end
  reply(command, current_status())
end)

commands.add_command("adaptive-ups-enable", "Claim control: SESSION FALLBACK_UPS", function(command)
  if not authorized(command) then return end
  local session, value = (command.parameter or ""):match("^(%x+) ([%d%.]+)$")
  local floor = parse_ups(value)
  if not session or #session ~= 32 or not floor then
    return reply(command, {error = "Expected 32 hex session and fallback UPS between 0.6 and 60"})
  end
  local s = state()
  s.enabled, s.floor, s.last_pulse = true, floor, game.tick
  s.owner = session
  game.speed = floor / 60
  write_roster()
  reply(command, {enabled = true, target_ups = game.speed * 60})
end)

commands.add_command("adaptive-ups-disable", "Disarm; leave current game speed unchanged", function(command)
  if not authorized(command) then return end
  state().enabled = false
  write_roster()
  reply(command, {enabled = false, target_ups = game.speed * 60})
end)

commands.add_command("adaptive-ups-speed", "Set target UPS while armed", function(command)
  if not authorized(command) then return end
  local session, value = (command.parameter or ""):match("^(%x+) ([%d%.]+)$")
  if not owner_matches(command, session) then return end
  local s, ups = state(), parse_ups(value)
  if not s.enabled or not ups or ups < s.floor then
    return reply(command, {error = "Arm first; target must be between fallback and 60 UPS"})
  end
  game.speed = ups / 60
  reply(command, {target_ups = game.speed * 60})
end)

commands.add_command("adaptive-ups-pulse", "Emit SESSION SEQUENCE progress marker", function(command)
  if not authorized(command) then return end
  local session, seq = (command.parameter or ""):match("^(%x+) (%d+)$")
  if not session or #session ~= 32 or #seq > 12 then
    return reply(command, {error = "Invalid pulse"})
  end
  local s = state()
  if s.enabled and not owner_matches(command, session) then return end
  if s.enabled then s.last_pulse = game.tick end
  local b = {version = 1, session = session, seq = tonumber(seq), tick = game.tick}
  local names = {}
  for _, player in pairs(game.connected_players) do
    names[#names + 1] = player.name
    b.player = player.name
    -- Each peer writes only its own marker when its simulation executes this
    -- command. No file or wall-clock value is ever read back into Lua.
    helpers.write_file("adaptive-ups/beacon.json", helpers.table_to_json(b), false, player.index)
  end
  b.player = nil
  table.sort(names)
  b.players, b.target_ups, b.enabled, b.owner = names, game.speed * 60, s.enabled, s.owner
  helpers.write_file("adaptive-ups/server.json", helpers.table_to_json(b), false, 0)
  reply(command, b)
end)

script.on_event(defines.events.on_player_joined_game, function()
  local s = state()
  if s.enabled then game.speed = math.min(game.speed, s.floor / 60) end
  write_roster()
end)

script.on_event(defines.events.on_player_left_game, function() write_roster() end)

script.on_nth_tick(30, function()
  local s = state()
  -- This is a simulation-time fail-safe: 180 ticks plus at most 30 ticks.
  -- At 15 UPS that is at most 14 seconds; it does not run while paused.
  if s.enabled and game.tick - s.last_pulse >= 180 then
    game.speed = math.min(game.speed, s.floor / 60)
  end
end)

script.on_init(function() state() end)
script.on_configuration_changed(function()
  local s = state()
  if s.enabled then game.speed = math.min(game.speed, s.floor / 60) end
  write_roster()
end)
