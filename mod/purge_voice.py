"""T99 ship-computer lines for the thruster purge (T93), rendered once offline and shipped in mod/voice/ (render:
tools/voice/render_purge_voice.py; licence: tools/voice/LICENSE-voice.md). Pure table; playback is
mod/voice_player.py, cueing is mod/ship_alarms.py.

User script 2026-10-20: "Warning! Submersion detected, thrusters flooded, follow manual purge procedure. Purge main
thruster in 3, 2, 1. Mains cleared. Purge retros in 3, 2, 1..." through up, down, starboard, port and boost, plus
the outcomes. Countdown lines start at a stage change and put "one" at ditch_purge.COUNTDOWN, when the band opens.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Line:
    clip: str
    text: str
    countdown: bool = False  # ends in "three, two, one" timed to the stage's countdown


PHRASE_GAP = .15  # s of silence at least between the phrase and "three"

LINES = (
    Line('warning', 'Warning! Submersion detected. Thrusters flooded. Follow manual purge procedure.'),
    Line('count_mains', 'Purge main thrusters in', True),
    Line('count_retro', 'Mains cleared. Purge retros in', True),
    Line('count_up', 'Retros cleared. Purge up thrusters in', True),
    Line('count_down', 'Up cleared. Purge down thrusters in', True),
    Line('count_starboard', 'Down cleared. Purge starboard in', True),
    Line('count_port', 'Starboard cleared. Purge port in', True),
    Line('count_boost', 'Port cleared. Boost in', True),
    Line('miss', 'Purge interrupted.'),
    Line('hull_half', 'Hull integrity fifty percent.'),
    Line('hull_quarter', 'Hull integrity critical.'),
    Line('relit', 'Purge complete. Thrusters relit.'),
    Line('wrecked', 'Hull failure. Abandon ship.'),
)
BY_CLIP = {line.clip: line for line in LINES}
# Lines that cut whatever we are saying: the countdowns are timed to the purge and the outcomes are the news.
# The rest (miss, hull) are dropped while another of our lines plays: said late, they describe a moment gone.
INTERRUPTS = frozenset(['warning', 'relit', 'wrecked']+[line.clip for line in LINES if line.countdown])


def countdown_clip(stage_name):
    """The countdown line that opens a purge stage (ditch_purge.STAGES names)."""
    return 'count_'+stage_name.lower()
