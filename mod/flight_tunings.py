"""Named per-class flight tunings (user design, 2026-10-22).

A tuning is one ship class's whole Sultan profile (space and air style) in a file named
<Class>_<Name>.json, for example Exotic_MyCustom.json, in <data>/flight-tunings. The Flight Tuning page
lists "Sultan's" first and then every file for the class being flown; picking one loads it. "Sultan's"
is the shipped class style (profile_defaults), baked into the build: it is never read from or written
to a file, so it is always there to go back to.

File content: {"format": "absolute-flight-tuning", "version": 1, "class": "Exotic", "name": "MyCustom",
"game_build": "180836", "values": {<profile key>: value, ...}}. Only "values" is required; keys it
leaves out keep Sultan's value, and unknown keys are ignored.
"""
import json
import os
import re
from pathlib import Path

from hotas_settings import PROFILE_CONTROLS, PROFILE_KEYS, SULTAN_TUNING, coerce, profile_defaults

FORMAT = 'absolute-flight-tuning'
VERSION = 1
# File-name prefix per class (SHIP_CLASSES ids).
CLASS_NAMES = {1: 'Hauler', 2: 'Fighter', 3: 'Explorer', 4: 'Shuttle', 6: 'Exotic', 7: 'Living', 8: 'Solar',
               9: 'Interceptor', 10: 'Corvette'}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._'()+-]{0,39}")
CONTROL_BY_KEY = {PROFILE_KEYS[c.id]: c for c in PROFILE_CONTROLS}


class FlightTunings:
    def __init__(self, folder, build=''):
        self.folder = Path(folder); self.build = str(build); self.issues = []

    def _files(self, class_id):
        """{name: path} for the class's tuning files; names that would shadow Sultan's are skipped."""
        prefix = CLASS_NAMES[class_id].lower() + '_'; found = {}
        if not self.folder.is_dir(): return found
        for path in self.folder.glob('*.json'):
            stem = path.stem
            if not stem.lower().startswith(prefix): continue
            name = stem[len(prefix):]
            if NAME.fullmatch(name) and name.lower() != SULTAN_TUNING.lower(): found[name] = path
        return found

    def names(self, class_id):
        return [SULTAN_TUNING] + sorted(self._files(class_id), key=str.lower)

    def profile(self, class_id, name):
        """The class profile a tuning sets: Sultan's with the file's values on top. ValueError when unusable."""
        if name == SULTAN_TUNING: return profile_defaults(class_id)
        path = self._files(class_id).get(name)
        if path is None: raise ValueError(f'no tuning file {CLASS_NAMES[class_id]}_{name}.json')
        try: data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error: raise ValueError(f'{path.name}: unreadable ({error})') from None
        if not isinstance(data, dict) or not isinstance(data.get('values'), dict):
            raise ValueError(f'{path.name}: no "values" section')
        if data.get('class') not in (None, CLASS_NAMES[class_id]):
            raise ValueError(f'{path.name}: written for {data["class"]}, not {CLASS_NAMES[class_id]}')
        profile = profile_defaults(class_id); self.issues = []
        for key, value in data['values'].items():
            control = CONTROL_BY_KEY.get(key)
            if control is None: self.issues.append(f'{path.name}: ignored unknown value {key}'); continue
            try: profile[key] = coerce(control, value)
            except ValueError as error: self.issues.append(f'{path.name}: {error}; using Sultan\'s')
        return profile

    def save(self, class_id, name, profile):
        """Write the profile as <Class>_<name>.json (replacing that tuning's file); returns the path."""
        if name == SULTAN_TUNING or not NAME.fullmatch(name): raise ValueError(f'cannot save a tuning named {name!r}')
        path = self._files(class_id).get(name) or self.folder / f'{CLASS_NAMES[class_id]}_{name}.json'
        self.folder.mkdir(parents=True, exist_ok=True)
        body = {'format': FORMAT, 'version': VERSION, 'class': CLASS_NAMES[class_id], 'name': name,
                'game_build': self.build, 'values': {k: profile[k] for k in sorted(CONTROL_BY_KEY) if k in profile}}
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(body, indent=1) + '\n', encoding='utf-8'); os.replace(tmp, path)
        return path

    def new_name(self, class_id):
        taken = {n.lower() for n in self._files(class_id)}
        name, n = 'Custom', 1
        while name.lower() in taken: n += 1; name = f'Custom {n}'
        return name
