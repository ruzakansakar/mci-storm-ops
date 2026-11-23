"""Which of the four screens a passenger has. One definition, used by the seed, the simulator, the agents and the metrics.

gate_screen and call_center apply to everyone. The app and SMS exist only if they are among the passenger's contact channels.
Email is a contact channel but not one of the four screens, so it is never counted."""
SCREENS = ("app", "sms", "gate_screen", "call_center")
PERSONAL = ("app", "sms")                    # screens that exist only for passengers who have them on file


def screens_for(contact_channels) -> tuple:
    have = set(contact_channels or ())
    return tuple(c for c in SCREENS if c not in PERSONAL or c in have)
