"""Pure classification: helicopter-related self deaths, never proof of piloting."""
import re

EXPLOSION = 'Meta.Progression.Context.Player.KillContext.VehicleExplosion'


def matches(obj):
    killer, victim = obj.get('killerSteamId'), obj.get('victimSteamId')
    cause, tags = obj.get('cause'), obj.get('contextTags')
    return (isinstance(killer,str) and re.fullmatch(r'[0-9]{17}',killer) is not None
            and killer == victim and isinstance(cause,str)
            and cause.startswith('Vehicle.Variant.Air.Rotary.')
            and isinstance(tags,list) and EXPLOSION in tags)
