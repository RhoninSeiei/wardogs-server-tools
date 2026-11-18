"""Persistent language-transition spacing for scheduled global announcements."""
TIMED_KINDS = frozenset(('periodic', 'network', 'kd', 'community', 'crash_advisory', 'signature_fun'))


def language(message, recipient, kind):
    if recipient or kind not in TIMED_KINDS:
        return None
    if message.startswith('[EN]'):
        return 'en'
    if message.startswith('[JA]'):
        return 'ja'
    return 'zh'


def ready(state, current_language, now, interval):
    previous = state.get('lastTimedAnnouncement')
    return (not current_language or not previous or previous['language'] == current_language
            or now >= previous['finishedAt'] + interval)


def record(state, current_language, finished):
    if current_language:
        state['lastTimedAnnouncement'] = {'language': current_language, 'finishedAt': finished}
