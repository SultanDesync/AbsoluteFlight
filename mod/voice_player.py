"""T99: plays the rendered purge lines (mod/voice/*.wav, mod/purge_voice.py) inside the game process.

The game's Wwise banks have no external-source events (soundbanksinfo.xml, build 180836), so the lines cannot go
through the game's mix: winmm PlaySound plays them from memory, asynchronously, on the default output in the
game's own audio session (the Windows mixer slider for NMS moves them too). One line at a time: a new line
replaces the one playing, as the ship computer does. Volume is ours (Ship Damage page, Voice volume), applied to
the samples. Worker thread only; no game memory.
"""
import array
import ctypes as C
import struct
import sys
import wave
from pathlib import Path

import purge_voice

VOICE_DIR = Path(__file__).resolve().parent/'voice'
SND_ASYNC, SND_NODEFAULT, SND_MEMORY = 0x1, 0x2, 0x4
VOLUME_STEPS = 20  # volume is rounded to 5 % so scaled lines can be kept


def _play_sound():
    winmm = C.WinDLL('winmm')
    winmm.PlaySoundW.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32]
    winmm.PlaySoundW.restype = C.c_int
    return winmm.PlaySoundW


def wav_image(rate, pcm):
    """A mono 16-bit RIFF image of pcm (little-endian samples)."""
    return (b'RIFF'+struct.pack('<I', 36+len(pcm))+b'WAVEfmt '+struct.pack('<IHHIIHH', 16, 1, 1, rate, 2*rate, 2, 16)
            +b'data'+struct.pack('<I', len(pcm))+pcm)


def scale(pcm, gain):
    """16-bit samples times gain (0..1)."""
    if gain >= 1.:return pcm
    samples = array.array('h'); samples.frombytes(pcm)
    if sys.byteorder != 'little':samples.byteswap()
    out = array.array('h', (int(s*gain) for s in samples))
    if sys.byteorder != 'little':out.byteswap()
    return out.tobytes()


class VoicePlayer:
    def __init__(self, folder=VOICE_DIR, play_sound=None):
        self.clips = {}
        for line in purge_voice.LINES:
            with wave.open(str(Path(folder)/f'{line.clip}.wav'), 'rb') as w:
                if (w.getnchannels(), w.getsampwidth(), w.getcomptype()) != (1, 2, 'NONE'):
                    raise ValueError(f'{line.clip}.wav is not mono 16-bit PCM')
                self.clips[line.clip] = (w.getframerate(), w.readframes(w.getnframes()))
        self.lengths = {clip: len(pcm)/2/rate for clip, (rate, pcm) in self.clips.items()}
        self.play_sound = play_sound or _play_sound()
        self.buffer = None   # the image playing: PlaySound reads it until the line ends
        self.scaled = {}     # (clip, volume step) -> pcm

    def say(self, clip, offset=0., volume=1.):
        """Play a line from offset s (a line resumed after a pause), cutting any line playing."""
        rate, pcm = self.clips[clip]
        step = max(0, min(VOLUME_STEPS, round(float(volume)*VOLUME_STEPS)))
        if step == 0:
            self.hush();return
        key = (clip, step)
        if key not in self.scaled:self.scaled[key] = scale(pcm, step/VOLUME_STEPS)
        start = 2*max(0, int(offset*rate))
        image = C.create_string_buffer(wav_image(rate, self.scaled[key][start:]))
        if not self.play_sound(C.addressof(image), None, SND_MEMORY | SND_ASYNC | SND_NODEFAULT):
            raise OSError('PlaySound refused the line '+clip)
        self.buffer = image  # the previous image is free once PlaySound has stopped it

    def hush(self):
        self.play_sound(None, None, 0)
        self.buffer = None
