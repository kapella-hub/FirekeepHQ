"""Redis scripts used by enrollment.

The credential secret never reaches this script. Only its SHA-256 hash is
registered, in the same atomic operation that spends the join ticket.
"""

ENROLL_CONSUME = r"""
local attempts = redis.call('INCR', KEYS[2])
if attempts == 1 then
  redis.call('EXPIRE', KEYS[2], 7200)
end
if attempts > tonumber(ARGV[1]) then
  return {'rate'}
end

if redis.call('EXISTS', KEYS[1]) == 0 then
  return {'unknown'}
end

local used_at = redis.call('HGET', KEYS[1], 'used_at')
if used_at and used_at ~= '' then
  local issued_hash = redis.call('HGET', KEYS[1], 'issued_key_hash')
  local issued_nonce = redis.call('HGET', KEYS[1], 'issued_device_nonce')
  local issued_credential_id = redis.call('HGET', KEYS[1], 'issued_credential_id') or ''
  local issued_device_id = redis.call('HGET', KEYS[1], 'issued_device_id') or ''
  if issued_hash == ARGV[4] and issued_nonce == ARGV[5] then
    if redis.call('EXISTS', KEYS[3]) == 0 then
      return {'credential_gone', used_at, issued_credential_id}
    end
    return {'replay', issued_credential_id, issued_device_id, used_at}
  end
  return {'used', used_at, issued_credential_id}
end

local expires_epoch = tonumber(redis.call('HGET', KEYS[1], 'expires_at_epoch') or '0')
if expires_epoch < tonumber(ARGV[2]) then
  return {'expired', redis.call('HGET', KEYS[1], 'expires_at') or ''}
end

local ticket_scopes_json = redis.call('HGET', KEYS[1], 'scopes') or '[]'
if ARGV[13] ~= '1' then
  return {'scope_violation', ticket_scopes_json}
end
local ticket_scopes = cjson.decode(ticket_scopes_json)
if type(ticket_scopes) ~= 'table' then
  return {'scope_violation', ticket_scopes_json}
end
local allowed_scopes = cjson.decode(ARGV[8])
local allowed = {}
for _, scope in ipairs(allowed_scopes) do allowed[scope] = true end
for key, scope in pairs(ticket_scopes) do
  if type(key) ~= 'number' or type(scope) ~= 'string' or not allowed[scope] then
    return {'scope_violation', ticket_scopes_json}
  end
end

-- The field map was built by auth.keys.build_credential_record from this
-- immutable ticket snapshot. Refuse a concurrent/manual ticket rewrite rather
-- than registering metadata that no longer describes the ticket being spent.
if ticket_scopes_json ~= ARGV[9] or
   (redis.call('HGET', KEYS[1], 'key_expires_days') or '0') ~= ARGV[10] then
  return {'ticket_changed'}
end

if redis.call('EXISTS', KEYS[3]) == 1 then
  return {'cred_exists'}
end

local fields = cjson.decode(ARGV[7])
for field, value in pairs(fields) do
  redis.call('HSET', KEYS[3], field, value)
end
local key_ttl = tonumber(ARGV[11])
if key_ttl > 0 then redis.call('EXPIRE', KEYS[3], key_ttl) end

redis.call('SET', KEYS[4], ARGV[4])
if key_ttl > 0 then redis.call('EXPIRE', KEYS[4], key_ttl) end
redis.call('ZADD', KEYS[5], tonumber(ARGV[2]), ARGV[6])
redis.call(
  'HSET', KEYS[1],
  'used_at', ARGV[3],
  'issued_credential_id', ARGV[6],
  'issued_device_id', ARGV[12],
  'issued_device_nonce', ARGV[5],
  'issued_key_hash', ARGV[4]
)
return {'ok', ARGV[6], ARGV[12]}
"""
