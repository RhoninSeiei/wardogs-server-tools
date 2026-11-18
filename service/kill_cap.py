"""Private absolute-kill policy; public messages disclose no numeric threshold."""
import math

KILL_LIMIT = 15
REASON = '不符合本服要求'


def eligible(player):
    value = player.get('kills')
    if isinstance(value, bool):
        return False
    try:
        kills = float(value)
        return math.isfinite(kills) and kills.is_integer() and kills >= KILL_LIMIT
    except (TypeError, ValueError, OverflowError):
        return False


def schedule(engine, player, now):
    if not engine.config.get('high_kill_kick') or not eligible(player):
        return False
    identity = str(player['steamId'])
    # A confirmed kick already has the stronger permanent rejoin policy.
    if engine.kick_store is not None:
        if engine.kick_store.kick_exempt(identity):
            return True
        with engine.kick_store.lock:
            if engine.kick_store.db.execute('SELECT 1 FROM rejoin_restrictions WHERE steam_id=?', (identity,)).fetchone():
                return True
    pending = engine.db.execute("SELECT 1 FROM jobs WHERE recipient=? AND kind='kill_cap_kick' AND status IN ('queued','sending','unknown')", (identity,)).fetchone()
    if not pending:
        engine.add_job(REASON, now, recipient=identity, kind='kill_cap_kick')
    return True
