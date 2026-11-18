"""Independent durable network advice rounds, called inside the poll transaction."""
import random

DEFAULTS = {
    'network_announcement_seconds': 1200,
    'network_announcement_jitter_seconds': 120,
    'network_announcement_stagger_seconds': 90,
    'network_messages': [],
    'community_announcement_seconds': 900,
    'community_messages': [],
}


def validate(config):
    for field in ('network_announcement_seconds', 'network_announcement_jitter_seconds',
                  'network_announcement_stagger_seconds'):
        value = config[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError('Invalid network announcement setting: ' + field)
    if config['network_announcement_seconds'] <= config['network_announcement_jitter_seconds']:
        raise ValueError('Network announcement interval must exceed jitter')
    interval = config['community_announcement_seconds']
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        raise ValueError('Invalid community announcement interval')
    messages = config['community_messages']
    if not isinstance(messages, list) or any(not isinstance(s, str) or not s or len(s) > 200 or any(not c.isprintable() for c in s) for s in messages):
        raise ValueError('Invalid community messages')
    messages = config['network_messages']
    if not isinstance(messages, list) or any(not isinstance(s, str) or not s or
            len(s) > 200 or any(not c.isprintable() for c in s) for s in messages):
        raise ValueError('Invalid network messages')


def next_round(config, now, last_global):
    center = config['network_announcement_seconds']
    jitter = config['network_announcement_jitter_seconds']
    period = config['global_announcement_seconds']
    gap = config['network_announcement_stagger_seconds']
    candidates = []
    for delay in range(center - jitter, center + jitter + 1):
        phase = (now + delay - last_global) % period
        if min(phase, period - phase) >= gap:
            candidates.append(delay)
    # Actual queue/recent-round checks still apply if configuration has no free slot.
    return now + random.choice(candidates or list(range(center - jitter, center + jitter + 1)))


def schedule(engine, now, players, global_due):
    config, state = engine.config, engine.state
    if not config['network_messages']:
        return
    if 'nextNetworkRound' not in state:
        state['nextNetworkRound'] = next_round(config, now, state['lastGlobalRound'])
    if not players or now < state['nextNetworkRound']:
        return
    if global_due or now - state['lastGlobalRound'] < config['network_announcement_stagger_seconds']:
        return
    if config['global_announcement_seconds'] - (now - state['lastGlobalRound']) < config['network_announcement_stagger_seconds']:
        return
    if engine.db.execute("SELECT 1 FROM jobs WHERE kind IN ('periodic','network') AND status IN ('queued','sending') LIMIT 1").fetchone():
        return
    for message in config['network_messages']:
        engine.add_job(message, now, kind='network')
    state['lastNetworkRound'] = now
    state['nextNetworkRound'] = next_round(config, now, state['lastGlobalRound'])


def schedule_community(engine, now, players):
    config, state = engine.config, engine.state
    if not config['community_messages']:
        return
    if 'lastCommunityRound' not in state:
        state['lastCommunityRound'] = now
    if not players or now - state['lastCommunityRound'] < config['community_announcement_seconds']:
        return
    if engine.db.execute("SELECT 1 FROM jobs WHERE kind='community' AND status IN ('queued','sending') LIMIT 1").fetchone():
        return
    for message in config['community_messages']:
        engine.add_job(message, now, kind='community')
    state['lastCommunityRound'] = now
