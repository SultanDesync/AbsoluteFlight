"""Rewrite the mod's game addresses and pinned bytes for a new NMS.exe, from a map made by game_port.py.

    python tools/game_port_apply.py REPO OLD_EXE NEW_EXE MAP_FILE [--write] [--decisions FILE]

Only code is touched: hex numbers and hex byte strings as Python sees them (tokenize). Comments and docstrings keep
their old addresses, as in earlier ports. Without --write nothing changes; the report says what would.

A hex number is rewritten only on evidence from the old exe:
- pinned: a hex byte string in the same statement holds exactly the old exe's bytes at that address;
- a data address (past .text), mapped by the code that refers to it;
- a code address of at least 0x100000 that is a function start, a call's return address or a branch target;
- 'weak': a code address that is only an instruction start (listed, so it can be checked);
- 'value': not an address but an offset inside a game object, at least 0x10000, that the update moved the same way
  everywhere it is used.
Everything else is left alone and, when it looks like it might matter, listed under REVIEW.

A hex byte string is re-read from the new exe when the old exe holds it at an address named in its statement (or, for
strings of 6 bytes and more, at exactly one place in .text). A string whose new bytes differ outside the address
fields is listed under CHANGED: the game's code changed there, and a patch written for the old bytes needs a look.

A number that is not pinned itself but equals a field the file's own pinned code shows moved follows that field.

--decisions FILE: JSON {"relative/path.py" or "*": {"0x733180": "value" | "rva" | "keep" | "=0x733220"}} overriding
the classification ("=..." gives the new number outright).
"""
import bisect
import glob
import io
import json
import os
import re
import sys
import tokenize

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import game_port
from iced_x86 import Decoder

HEX = re.compile(r'^[0-9a-fA-F]{2}(?: ?[0-9a-fA-F]{2})*$')


class Porter:
    def __init__(self, old_path, new_path, map_path):
        self.old = game_port.Image(old_path); self.new = game_port.Image(new_path); self.map = game_port.load(map_path)
        self.starts = {b for b, e in self.old.functions}
        self.chunks = self.old.chunks(); self.chunk_starts = [c[0] for c in self.chunks]
        self.decoder = Decoder(64, self.old.data, ip=0); self.new_decoder = Decoder(64, self.new.data, ip=0); self._bounds = {}
        self.rdata = next(a for n, a, b, _ in self.old.sections if n == '.rdata')

    def boundary(self, rva):
        k = bisect.bisect_right(self.chunk_starts, rva)-1
        if k < 0 or not self.chunks[k][0] <= rva < self.chunks[k][1]:return None
        if k not in self._bounds:
            b, e, _ = self.chunks[k]; s = set(); d = self.decoder; d.position = b; d.ip = b
            while d.position < e:s.add(d.decode().ip)
            self._bounds[k] = s
        return rva in self._bounds[k]

    def evidence(self, v):
        text = self.old.text; ev = set()
        if text[0] <= v < text[1]:
            if v in self.starts:ev.add('func')
            else:
                b = self.boundary(v); ev.add('insn' if b else 'mid')
            if self.old.data[v-5] == 0xE8 and self.boundary(v-5):ev.add('ret')
            if v in self.map.targets:ev.add('target')
        elif v >= self.rdata:ev.add('data')
        votes = self.map.values.get(v)
        if votes:
            best = max(votes.values()); total = sum(votes.values())
            ev.add('value' if best == total and total >= 3 else 'value-unsure')
        return ev

    def where(self, rva):
        """The map's answer, or for an address inside a chunk the update changed, the instruction-level alignment."""
        new, how = self.map.where(rva)
        if new is None and how.startswith('in a chunk that changed'):
            aligned = self.align(rva)
            if aligned:return aligned
        return new, how

    def _instructions(self, image, decoder, begin, end):
        """[(ip, loosely blanked bytes)] of a chunk."""
        blanked, _, _ = game_port.blank(image, decoder, begin, end, True)
        out = []; decoder.position = begin; decoder.ip = begin
        while decoder.position < end:
            ins = decoder.decode()
            if ins.ip+ins.len > end:break
            out.append((ins.ip, blanked[ins.ip-begin:ins.ip-begin+ins.len]))
        return out

    def _alignment(self, chunk):
        """For a chunk the update changed: ({old instruction ip: new ip}, ratio, new chunk), from the new chunk between
        the same matched neighbours whose instructions agree best. Cached."""
        cache = self.__dict__.setdefault('_aligned', {})
        if chunk in cache:return cache[chunk]
        import difflib
        m = self.map; b, e, _ = chunk
        k = bisect.bisect_right(m.starts, b)-1
        low = m.matched[k][2]+(m.matched[k][1]-m.matched[k][0]) if k >= 0 else self.new.text[0]
        high = m.matched[k+1][2] if k+1 < len(m.matched) else self.new.text[1]
        mine = self._instructions(self.old, self.decoder, b, e); best = None
        for nb, ne, _ in m.record['added']:
            if nb < low or ne > high:continue
            theirs = self._instructions(self.new, self.new_decoder, nb, ne)
            sm = difflib.SequenceMatcher(None, [x[1] for x in mine], [x[1] for x in theirs], autojunk=False)
            ratio = sm.ratio()
            if best is None or ratio > best[1]:
                pairs = {}
                for block in sm.get_matching_blocks():
                    for i in range(block.size):pairs[mine[block.a+i][0]] = theirs[block.b+i][0]
                best = (pairs, ratio, (nb, ne))
        cache[chunk] = best
        return best

    def align(self, rva):
        chunk = self.map.changed_chunk(rva)
        if not chunk:return None
        found = self._alignment(chunk)
        if not found or found[1] < .6:return None
        pairs, ratio, _ = found
        for back in range(16):   # the instruction that holds this address
            if rva-back in pairs:
                return pairs[rva-back]+back, f'aligned in a changed function ({ratio:.2f} alike)'
        return None

    def moved_bytes(self, old_at, new_at, size):
        """How the bytes [old_at, old_at+size) differ in the new build: (offsets that differ, True when every one is
        an address field or a struct field that moved, {old value: new value} for those struct fields and constants).
        None when part of the range cannot be compared instruction for instruction. The range may span chunks."""
        a = self.old.data[old_at:old_at+size]; b = self.new.data[new_at:new_at+size]
        diff = [i for i in range(size) if a[i] != b[i]]
        if not diff:return [], True, {}
        covered = set(); moved = {}; pos = old_at
        while pos < old_at+size:
            m = self.map.chunk(pos)
            if m:
                ob, oe, nb = m[0], m[1], m[2]; ne = nb+(oe-ob)
                def shift(x, ob=ob, nb=nb):return x-ob+nb
            else:
                chunk = self.map.changed_chunk(pos); found = self._alignment(chunk) if chunk else None
                if not found:return None
                ob, oe = chunk[:2]; nb, ne = found[2]
                def shift(x, pairs=found[0]):
                    for back in range(16):
                        if x-back in pairs:return pairs[x-back]+back
                    return None
            _, fields, values = game_port.blank(self.old, self.decoder, ob, oe, True)
            _, _, values2 = game_port.blank(self.new, self.new_decoder, nb, ne, True)
            fresh = {nb+at: value for at, value in values2}
            for at, _ in fields:covered.update(range(ob+at-old_at, ob+at-old_at+4))
            for at, value in values:
                rel = ob+at-old_at
                if not -4 < rel < size:continue
                there = shift(ob+at)
                other = fresh.get(there) if there is not None else None
                if other is not None and other != value:
                    moved[value] = other; covered.update(range(rel, rel+4))
            pos = oe
        return diff, all(i in covered for i in diff), moved

def scan(porter, path, rel, decisions, known=None):
    """[(row, col, old text, new text, kind, note)] edits, and report lines, for one file."""
    with open(path, encoding='utf-8', newline='') as f:source = f.read()
    edits = []; review = []; changed = []; weak = []; patched = []
    statement = []
    old = porter.old; new = porter.new; where = porter.where; moved_values = dict(known or {}); loose_strings = []
    override = {**decisions.get('*', {}), **decisions.get(rel, {})}

    def flush():
        numbers = [t for t in statement if t[0] == 'n']; pinned = set()
        for k, t in enumerate(statement):
            if t[0] != 's':continue
            _, row, col, text, raw = t; size = len(raw)
            found = associate(old, statement, k)
            if not found:
                loose_strings.append((row, col, text, raw)); continue
            p, at = found
            if p is not None:pinned.add((p[1], p[2]))
            target, how = where(at)
            if target is None:
                changed.append(f'{rel}:{row} bytes {text[:24]} at {at:#x}: {how}'); continue
            last, _ = where(at+size-1)
            if last != target+size-1:
                changed.append(f'{rel}:{row} bytes {text[:24]} at {at:#x}: the new code does not line up over the whole string (code was added or removed inside it)'); continue
            fresh = new.data[target:target+size]
            moved = porter.moved_bytes(at, target, size)
            if fresh != raw:
                edits.append((row, col, text, restring(text, fresh), 'bytes', ''))
                if moved is None or not moved[1]:
                    changed.append(f'{rel}:{row} bytes at {at:#x} -> {target:#x} differ in more than addresses and fields: {raw.hex()} -> {fresh.hex()}')
                if moved:moved_values.update(moved[2])
        for _, row, col, text, v in numbers:
            ev = porter.evidence(v); choice = override.get(f'{v:#x}')
            if choice is None and v in moved_values and (row, col) not in pinned:choice = '=%#x' % moved_values[v]
            target, how = where(v)
            is_pinned = (row, col) in pinned
            if choice is None:
                if is_pinned or 'data' in ev:choice = 'rva'
                elif v >= 0x100000 and ev & {'func', 'ret', 'target'}:choice = 'rva' if not ev & {'value', 'value-unsure'} else 'review'
                elif v >= 0x100000 and 'insn' in ev:choice = 'weak' if not ev & {'value', 'value-unsure'} else 'review'
                elif 'value' in ev and v >= 0x10000:choice = 'value'
                elif ev & {'value', 'value-unsure'}:choice = 'review'
                elif v >= 0x100000 and old.text[0] <= v < old.text[1]:choice = 'review'
                else:choice = 'keep'
            if choice.startswith('='):edits.append((row, col, text, restyle(text, int(choice[1:], 16)), 'value', 'pinned field')); continue
            if choice in ('rva', 'weak'):
                if target is None:review.append(f'{rel}:{row} {text} address not mapped: {how}'); continue
                if choice == 'weak':weak.append(f'{rel}:{row} {text} -> {target:#x} ({how})')
                edits.append((row, col, text, restyle(text, target), 'rva', how))
            elif choice == 'value':
                votes = porter.map.values.get(v)
                if not votes:review.append(f'{rel}:{row} {text} marked value but no vote'); continue
                edits.append((row, col, text, restyle(text, max(votes, key=votes.get)), 'value', ''))
            elif choice == 'review':
                votes = porter.map.values.get(v, {})
                review.append(f"{rel}:{row} {text} {sorted(ev)} rva->{'%#x' % target if target is not None else '?'} "
                              f"value->{ {hex(k): c for k, c in sorted(votes.items(), key=lambda x: -x[1])[:3]} }")
        statement.clear()

    def carried():
        # A patch's own bytes hold an object offset as four little-endian bytes. Where the update moved that offset
        # the same way in every place the game uses it (or in this file's pinned code), the patch follows.
        for row, col, text, raw in loose_strings:
            if len(raw) < 5:continue
            fresh = bytearray(raw)
            for i in range(len(raw)-3):
                value = int.from_bytes(raw[i:i+4], 'little')
                if value < 0x10000:continue
                to = file_values.get(value)
                if to is None:
                    votes = porter.map.values.get(value)
                    if votes and len(votes) == 1 and sum(votes.values()) >= 3:to = next(iter(votes))
                if to is not None:fresh[i:i+4] = to.to_bytes(4, 'little'); patched.append(f'{rel}:{row} {value:#x} -> {to:#x} in {text[:60]}')
            if bytes(fresh) != raw:edits.append((row, col, text, restring(text, bytes(fresh)), 'bytes', 'patch bytes'))

    def finish():
        carried()
        # A patch's own bytes (never in the old exe) that carry a field the update moved: the patch needs a new offset.
        for row, col, text, raw in loose_strings:
            for value, fresh in file_values.items():
                if len(raw) >= 4 and 0x100 <= abs(value) < 0x10000 and (value & 0xFFFFFFFF).to_bytes(4, 'little') in raw:
                    changed.append(f'{rel}:{row} patch bytes {text[:40]} hold {value:#x}, which the pinned code of this file now has as {fresh:#x}')

    file_values = {}
    for found in statements(source, old.size):
        statement[:] = found; flush(); file_values.update(moved_values)
    finish()
    return source, edits, review, changed, weak, file_values, patched


def associate(image, statement, k):
    """Where the image holds the hex string statement[k]: (the number token that names the place or None, RVA), or
    None. First the nearest number before it (the string starts there, or follows the strings between them); then,
    for 4 bytes and more, any number of the statement up to 0x400 bytes before the string; then, for 6 bytes and
    more, the one place in .text that holds it."""
    raw = statement[k][4]; size = len(raw); data = image.data; run = 0
    for back in range(k-1, -1, -1):
        p = statement[back]
        if p[0] == 's':run += len(p[4]); continue
        v = p[4]
        for offset in ((run, 0) if run else (0,)):
            if data[v+offset:v+offset+size] == raw:return p, v+offset
        if size < 4:return None
    if size >= 4:
        for p in statement:
            if p[0] != 'n':continue
            at = data.find(raw, p[4], p[4]+0x400+size)
            if at >= 0:return p, at
    if size >= 6:
        at = data.find(raw, image.text[0], image.text[1])
        if at >= 0 and data.find(raw, at+1, image.text[1]) < 0:return None, at
    return None


def statements(source, size):
    """The hex numbers ('n', row, col, text, value) and hex byte strings ('s', row, col, text, bytes) of each logical line."""
    statement = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        kind, text, (row, col) = token.type, token.string, token.start
        if kind == tokenize.NUMBER and text[:2].lower() == '0x' and '_' not in text:
            v = int(text, 16)
            if 0x1000 <= v < size:statement.append(('n', row, col, text, v))
        elif kind == tokenize.STRING and text[0] in ('"', "'") and len(text) >= 4 and text[1] != text[0] and HEX.match(text[1:-1]):
            statement.append(('s', row, col, text, bytes.fromhex(text[1:-1])))
        elif kind == tokenize.NEWLINE:
            yield statement; statement = []
    yield statement


def audit(repo, exe):
    """Which hex byte strings of the sources the exe holds where their statement says: prints the ones it does not."""
    image = game_port.Image(exe); held = 0; loose = []
    for path in sorted(glob.glob(os.path.join(repo, 'mod', '**', '*.py'), recursive=True)+glob.glob(os.path.join(repo, 'tools', '*.py'))):
        if os.path.basename(path).startswith('game_port'):continue
        rel = os.path.relpath(path, repo).replace(os.sep, '/')
        with open(path, encoding='utf-8', newline='') as f:source = f.read()
        for statement in statements(source, image.size):
            for k, t in enumerate(statement):
                if t[0] != 's':continue
                if associate(image, statement, k):held += 1
                else:loose.append(f'{rel}:{t[1]} {t[3][:50]}')
    print(f'{held} byte strings are in the exe where their statement says; {len(loose)} are not:')
    for row in loose:print('  '+row)


def restring(text, raw):
    """The hex string token text with the bytes raw, keeping its quotes, case and spacing."""
    body = raw.hex(' ') if ' ' in text else raw.hex()
    return text[0]+(body.upper() if text[1:-1].upper() == text[1:-1] and not text[1:-1].replace(' ', '').isdigit() else body)+text[0]


def restyle(text, value):
    digits = text[2:]; out = f'{value:X}' if digits.upper() == digits and not digits.isdigit() else f'{value:x}'
    if digits.isdigit():out = f'{value:X}'
    return text[:2]+out.zfill(len(digits) if digits.startswith('0') else 0)


def rewrite(source, edits):
    lines = source.split('\n')
    for row, col, text, fresh, _, _ in sorted(set(e[:4]+('', '') for e in edits), reverse=True):
        line = lines[row-1]
        if line[col:col+len(text)] != text:raise RuntimeError(f'token moved at {row}:{col}')
        lines[row-1] = line[:col]+fresh+line[col+len(text):]
    return '\n'.join(lines)


def main(argv):
    if len(argv) == 4 and argv[1] == 'audit':return audit(argv[2], argv[3])
    args = [a for a in argv[1:] if not a.startswith('--')]; write = '--write' in argv
    decisions = {}
    if '--decisions' in argv:
        with open(argv[argv.index('--decisions')+1], encoding='utf-8') as f:decisions = json.load(f)
        args.remove(argv[argv.index('--decisions')+1])
    repo, old_path, new_path, map_path = args
    porter = Porter(old_path, new_path, map_path)
    files = sorted(glob.glob(os.path.join(repo, 'mod', '**', '*.py'), recursive=True)+glob.glob(os.path.join(repo, 'tools', '*.py')))
    totals = {'rva': 0, 'value': 0, 'bytes': 0}; reviews = []; changes = []; weaks = []; fields = []; touched = 0; hows = {}; odd = []; patches = []
    for path in files:
        if os.path.basename(path).startswith('game_port'):continue
        rel = os.path.relpath(path, repo).replace(os.sep, '/')
        file_values = scan(porter, path, rel, decisions)[5]   # first pass: the fields this file's pinned code shows moved
        source, edits, review, changed, weak, file_values, patched = scan(porter, path, rel, decisions, file_values)
        patches += patched
        if file_values:fields.append(rel+': '+', '.join(f'{a:#x}->{b:#x}' for a, b in sorted(file_values.items())))
        edits = [e for e in edits if e[2] != e[3]]
        for e in edits:
            totals[e[4]] += 1; hows[e[5]] = hows.get(e[5], 0)+1
            if e[5] in ('between', 'code end') or e[5].startswith('aligned'):odd.append(f'{rel}:{e[0]} {e[2]} -> {e[3]} ({e[5]})')
        reviews += review; changes += changed; weaks += weak
        if edits:
            touched += 1
            if write:
                with open(path, 'w', encoding='utf-8', newline='') as f:f.write(rewrite(source, edits))
    print(f"{'wrote' if write else 'would change'} {touched} files: {totals['rva']} addresses, {totals['value']} object offsets, {totals['bytes']} byte strings")
    print('how: '+', '.join(f'{k or "bytes"} {v}' for k, v in sorted(hows.items())))
    for title, rows in (('PATCH BYTES that follow a moved offset', patches), ('MAPPED INDIRECTLY (check)', odd), ('FIELDS that moved in pinned code', fields), ('WEAK (instruction start only)', weaks), ('REVIEW', reviews), ('CHANGED', changes)):
        print(f'\n{title}: {len(rows)}')
        for r in rows:print('  '+r[:330])


if __name__ == '__main__':
    main(sys.argv)
