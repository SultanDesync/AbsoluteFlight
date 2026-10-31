"""Keyboard source for keyboard flight (T112, D26): key states as digital tokens for the button router.

Polled once per runtime tick with GetAsyncKeyState, like the menu's key capture. Not a low-level keyboard
hook: Windows holds every keystroke (the game's too) until a hook returns, and a Python hook can wait on the
GIL, which would add input lag to the whole game. Keys our own key lane (D20) is holding are skipped, so
the mod never reads back what it sends. Presses and releases are counted from level changes, as the router's
press rules expect (mod/hotas_buttons.py).

read() answers only while the source is live: NMS focused, piloting, no HOTAS menu or capture open. Otherwise
it returns None, the router releases everything, and a key still held must be let go before it fires again.
"""
import re

from hotas_buttons import KEYBOARD_TOKEN,game_ship_keys


class KeyboardSource:
    def __init__(self):
        self.keys={}  # vk -> [down, presses, releases]
        self.live=False

    def poll(self,key_down,vks,live,exclude=()):
        """key_down(vk) -> bool; vks: the keys anything is bound to; exclude: keys our key lane holds."""
        self.live=bool(live)
        for vk in vks:
            down=bool(key_down(vk)) and vk not in exclude
            state=self.keys.setdefault(vk,[down,0,0])
            if down and not state[0]:state[1]+=1
            elif state[0] and not down:state[2]+=1
            state[0]=down

    def read(self,token):
        """(down, presses, releases, generation) for a keyboard token, or None while not live or never polled."""
        if not self.live:return None
        m=KEYBOARD_TOKEN.fullmatch(token)
        state=self.keys.get(int(m[1],16)) if m else None
        return None if state is None else (state[0],state[1],state[2],1)


def lane_keys(lane):
    """The VKs a key lane (mod/key_lane.KeyLane) is holding down right now."""
    return {target for kind,target in getattr(lane,'down',{}).values() if kind=='key'}


class GameBindings:
    """The game's PC key remaps (T112 collision warning), read-only.

    The game keeps them in Binaries/SETTINGS/GCUSERSETTINGSDATA.MXML, CustomBindingsPC: one GcInputActionMapping2
    per remapped action with ActionSet, Action and Button (seen on DADS 2026-10-30). The file is only read, never
    written (AGENTS.md: agents never write Binaries), and re-read only when it changes.
    """
    EMPTY='<Property name="CustomBindingsPC" />'
    BLOCK=re.compile(r'<Property name="CustomBindingsPC">(.*?)\n\t</Property>',re.S)
    ENTRY=re.compile(r'name="ActionSet" value="([^"]*)".*?name="Action" value="([^"]*)".*?name="Button" value="([^"]*)"',re.S)

    def __init__(self,path):
        self.path=path;self.stamp=None;self.state=None

    def remaps(self):
        """[(action set, action, key name)], [] for stock bindings, None when the file can't be read."""
        try:stamp=self.path.stat().st_mtime_ns
        except OSError:self.stamp=None;self.state=None;return None
        if stamp!=self.stamp:
            self.stamp=stamp
            try:text=self.path.read_text(encoding='utf-8',errors='replace')
            except OSError:self.state=None;return None
            block=self.BLOCK.search(text)
            self.state=[] if self.EMPTY in text else self.ENTRY.findall(block[1]) if block else None
        return self.state

    def ship_keys(self):
        """{vk: labels} the game uses while you fly, with the player's remaps; None when unreadable."""
        remaps=self.remaps()
        return None if remaps is None else game_ship_keys(remaps)


def game_settings_path():
    """GCUSERSETTINGSDATA.MXML next to the running NMS.exe, or None outside the game."""
    import ctypes as C
    from pathlib import Path
    buffer=C.create_unicode_buffer(32768)
    if not C.windll.kernel32.GetModuleFileNameW(None,buffer,len(buffer)):return None
    exe=Path(buffer.value)
    return exe.parent/'SETTINGS'/'GCUSERSETTINGSDATA.MXML' if exe.name.lower()=='nms.exe' else None
