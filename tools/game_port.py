"""Carry the mod's game addresses from one NMS.exe build to the next.

    python tools/game_port.py map OLD_EXE NEW_EXE MAP_FILE     match every function of the old exe to the new one
    python tools/game_port.py lookup MAP_FILE 0x917200 ...     where an old RVA went

How the match works. Both executables are cut into chunks: every .pdata function range, and the gaps between them
(leaf functions, padding). Each chunk is disassembled and the bytes that move when code or data shifts are blanked:
RIP-relative displacements, rel32 branches leaving the chunk, and 64-bit immediates that point into the image.
Chunks whose blanked bytes are identical are the same code. A chunk that is unique in both builds is an anchor;
chunks between two anchors are paired in order when their blanked bytes agree.

A second, looser pass takes what is left between the anchors and also blanks struct displacements and 32-bit
immediates. A pair found that way is the same code with a field that moved inside a game object (the update grew a
structure); the old and new values of every such field are recorded. What is still left changed in the update (or
is new, or gone) and is reported, never guessed.

Every blanked address field of a matched pair also says where its target went (old target -> new target). Those
votes map data addresses (globals) and leaf functions that no chunk of their own covers.

Needs pefile and iced_x86 (both ship in the mod's Python).
"""
import bisect
import collections
import difflib
import pickle
import struct
import sys

import pefile
from iced_x86 import Decoder, OpKind


class Image:
    def __init__(self, path):
        pe = pefile.PE(path, fast_load=True)
        self.base = pe.OPTIONAL_HEADER.ImageBase
        self.size = pe.OPTIONAL_HEADER.SizeOfImage
        data = bytearray(self.size)
        self.sections = []
        for s in pe.sections:
            raw = s.get_data()
            data[s.VirtualAddress:s.VirtualAddress+len(raw)] = raw
            self.sections.append((s.Name.rstrip(b'\0').decode(), s.VirtualAddress, s.VirtualAddress+s.Misc_VirtualSize, s.Characteristics))
        self.data = bytes(data)
        self.text = next((a, b) for n, a, b, _ in self.sections if n == '.text')
        directory = pe.OPTIONAL_HEADER.DATA_DIRECTORY[3]
        table = self.data[directory.VirtualAddress:directory.VirtualAddress+directory.Size]
        self.functions = sorted({(b, e) for b, e, _ in struct.iter_unpack('<III', table[:len(table)//12*12])
                                 if self.text[0] <= b < e <= self.text[1]})

    def chunks(self):
        """[(begin, end, is_function)] covering .text: .pdata ranges, and the gaps between them split at padding."""
        out = []; at = self.text[0]
        for b, e in self.functions:
            if b < at:continue            # overlapping record: the earlier one already covers it
            if b > at:out += self._gap(at, b)
            out.append((b, e, True)); at = e
        if at < self.text[1]:out += self._gap(at, self.text[1])
        return out

    def _gap(self, a, b):
        """Leaf functions between two .pdata ranges: runs of code separated by int3 padding up to a 16-byte boundary."""
        out = []; data = self.data; at = a
        while at < b:
            while at < b and data[at] == 0xCC:at += 1
            if at >= b:break
            end = at
            while True:
                stop = data.find(b'\xcc', end, b)
                if stop < 0:end = b; break
                run = stop
                while run < b and data[run] == 0xCC:run += 1
                if run >= b or run % 16 == 0:end = stop; break
                end = run
            out.append((at, end, False)); at = end
        return out


def blank(image, decoder, begin, end, loose=False):
    """(the chunk with moving fields zeroed, [(offset in chunk, target RVA)], [(offset in chunk, value)]).
    loose: struct displacements and 32-bit immediates are zeroed as well and listed as values, so a chunk whose only
    change is a field that moved inside a game object still compares equal."""
    data = image.data; base = image.base; size = image.size
    out = bytearray(data[begin:end]); fields = []; values = []
    decoder.position = begin; decoder.ip = begin
    while decoder.position < end:
        ins = decoder.decode()
        if ins.is_invalid:continue
        ip = ins.ip
        if ip+ins.len > end:break
        o = None
        if ins.is_ip_rel_memory_operand:
            o = decoder.get_constant_offsets(ins)
            if o.displacement_size == 4:
                at = ip-begin+o.displacement_offset; out[at:at+4] = bytes(4); fields.append((at, ins.ip_rel_memory_address))
        elif ins.op0_kind == OpKind.NEAR_BRANCH64:
            target = ins.near_branch_target
            if not begin <= target < end:
                o = decoder.get_constant_offsets(ins)
                if o.immediate_size == 4:
                    at = ip-begin+o.immediate_offset; out[at:at+4] = bytes(4); fields.append((at, target))
            continue
        elif ins.op_count == 2 and ins.op1_kind == OpKind.IMMEDIATE64:
            value = ins.immediate64
            if base <= value < base+size:
                o = decoder.get_constant_offsets(ins)
                at = ip-begin+o.immediate_offset; out[at:at+8] = bytes(8); fields.append((at, value-base))
            continue
        if not loose:continue
        if o is None:o = decoder.get_constant_offsets(ins)
        if o.has_displacement and not ins.is_ip_rel_memory_operand and o.displacement_size in (1, 4):
            n = o.displacement_size; at = ip-begin+o.displacement_offset
            values.append((at, int.from_bytes(out[at:at+n], 'little', signed=True))); out[at:at+n] = bytes(n)
        if o.has_immediate and o.immediate_size == 4:
            at = ip-begin+o.immediate_offset
            values.append((at, int.from_bytes(out[at:at+4], 'little'))); out[at:at+4] = bytes(4)
    return bytes(out), fields, values


def analyse(image):
    decoder = Decoder(64, image.data, ip=0)
    chunks = image.chunks(); blanked = []; fields = []
    for begin, end, _ in chunks:
        b, f, _ = blank(image, decoder, begin, end)
        blanked.append(b); fields.append(f)
    return chunks, blanked, fields


def match(old, new):
    """({old chunk index: new chunk index}, anchors that moved, the in-order chain of anchors)."""
    (oc, ob, _), (nc, nb, _) = old, new
    count_old = collections.Counter(ob); count_new = collections.Counter(nb)
    where_new = {b: i for i, b in enumerate(nb) if count_new[b] == 1}
    anchors = [(i, where_new[b]) for i, b in enumerate(ob) if count_old[b] == 1 and b in where_new and len(b) >= 8]
    # Keep the longest run of anchors that is in the same order in both builds (a moved function is not a bound).
    tails = []; links = [None]*len(anchors); ends = []
    for k, (_, j) in enumerate(anchors):
        p = bisect.bisect_left(tails, j)
        if p == len(tails):tails.append(j); ends.append(k)
        else:tails[p] = j; ends[p] = k
        links[k] = ends[p-1] if p else None
    chain = []; k = ends[-1] if ends else None
    while k is not None:chain.append(anchors[k]); k = links[k]
    chain.reverse()
    pairs = dict(chain); moved = [a for a in anchors if pairs.get(a[0]) != a[1]]
    # Between consecutive anchors: pair what is left in order, from both ends, while the blanked bytes agree.
    bounds = [(-1, -1)]+chain+[(len(oc), len(nc))]
    for (i0, j0), (i1, j1) in zip(bounds, bounds[1:]):
        i, j = i0+1, j0+1
        while i < i1 and j < j1 and ob[i] == nb[j]:pairs[i] = j; i += 1; j += 1
        a, b = i1-1, j1-1
        while a >= i and b >= j and ob[a] == nb[b]:pairs[a] = b; a -= 1; b -= 1
    for i, j in moved:pairs.setdefault(i, j)   # unique in both builds, only somewhere else: still the same code
    return pairs, moved, chain


def loose_pairs(old_image, new_image, old, new, pairs, chain):
    """Second pass over what the first left: between consecutive anchors, pair chunks whose loosely blanked bytes
    agree. {old index: (new index, old fields, new fields, old values, new values)}."""
    od = Decoder(64, old_image.data, ip=0); nd = Decoder(64, new_image.data, ip=0)
    taken = set(pairs.values()); out = {}
    bounds = [(-1, -1)]+chain+[(len(old[0]), len(new[0]))]
    for (i0, j0), (i1, j1) in zip(bounds, bounds[1:]):
        left = [i for i in range(i0+1, i1) if i not in pairs]
        if not left:continue
        right = [j for j in range(j0+1, j1) if j not in taken]
        if not right:continue
        a = [blank(old_image, od, old[0][i][0], old[0][i][1], True) for i in left]
        b = [blank(new_image, nd, new[0][j][0], new[0][j][1], True) for j in right]
        sm = difflib.SequenceMatcher(None, [x[0] for x in a], [x[0] for x in b], autojunk=False)
        for block in sm.get_matching_blocks():
            for k in range(block.size):
                x = a[block.a+k]; y = b[block.b+k]
                out[left[block.a+k]] = (right[block.b+k], x[1], y[1], x[2], y[2])
    return out


def build(old_path, new_path):
    old_image, new_image = Image(old_path), Image(new_path)
    old = analyse(old_image); new = analyse(new_image)
    pairs, moved, chain = match(old, new)
    votes = collections.defaultdict(collections.Counter)
    for i, j in pairs.items():
        for (at, target), (at2, target2) in zip(old[2][i], new[2][j]):
            if at == at2:votes[target][target2] += 1
    loose = loose_pairs(old_image, new_image, old, new, pairs, chain)
    values = collections.defaultdict(collections.Counter)   # a field or constant that changed: old value -> new values
    for i, (j, f0, f1, v0, v1) in loose.items():
        for (at, target), (at2, target2) in zip(f0, f1):
            if at == at2:votes[target][target2] += 1
        for (at, value), (at2, value2) in zip(v0, v1):
            if at == at2 and value != value2:values[value][value2] += 1
    targets = {}; contested = {}
    for target, counter in votes.items():
        best = counter.most_common(2)
        targets[target] = best[0][0]
        if len(best) > 1:contested[target] = dict(counter)
    chunks = old[0]
    matched = sorted([(chunks[i][0], chunks[i][1], new[0][j][0], 'code') for i, j in pairs.items()]
                     + [(chunks[i][0], chunks[i][1], new[0][v[0]][0], 'code, fields moved') for i, v in loose.items()])
    changed = sorted((b, e, f) for i, (b, e, f) in enumerate(chunks) if i not in pairs and i not in loose)
    new_matched = set(pairs.values()) | {v[0] for v in loose.values()}
    added = sorted((b, e, f) for j, (b, e, f) in enumerate(new[0]) if j not in new_matched)
    return {'old': {'path': old_path, 'size': old_image.size, 'sections': old_image.sections},
            'new': {'path': new_path, 'size': new_image.size, 'sections': new_image.sections},
            'matched': matched, 'changed': changed, 'added': added, 'targets': targets, 'contested': contested,
            'values': {k: dict(v) for k, v in values.items()}, 'moved': len(moved)}


class Map:
    """Where an old RVA went. where(rva) -> (new RVA or None, how): 'code' inside a matched chunk ('code, fields moved'
    when the chunk matched only loosely), 'target' named by matched code that refers to it, 'between' two mapped
    neighbours that moved by the same amount (data only), or None with the reason."""
    def __init__(self, record):
        self.record = record; self.matched = record['matched']; self.starts = [m[0] for m in self.matched]
        self.targets = record['targets']; self.contested = record['contested']; self.values = record['values']
        self.changed = record['changed']; self.changed_starts = [c[0] for c in self.changed]
        self.keys = sorted(self.targets)
        self.text = next((a, b) for n, a, b, _ in record['old']['sections'] if n == '.text')

    def chunk(self, rva):
        k = bisect.bisect_right(self.starts, rva)-1
        if k >= 0 and self.matched[k][0] <= rva < self.matched[k][1]:return self.matched[k]
        return None

    def changed_chunk(self, rva):
        k = bisect.bisect_right(self.changed_starts, rva)-1
        if k >= 0 and self.changed[k][0] <= rva < self.changed[k][1]:return self.changed[k]
        return None

    def where(self, rva):
        m = self.chunk(rva)
        if m:return rva-m[0]+m[2], m[3]
        if rva in self.targets:
            return (self.targets[rva], 'target') if rva not in self.contested else (None, 'contested '+str(self.contested[rva]))
        k = bisect.bisect_right(self.starts, rva)-1
        if k >= 0 and rva == self.matched[k][1]:return rva-self.matched[k][0]+self.matched[k][2], 'code end'
        if self.text[0] <= rva < self.text[1]:
            c = self.changed_chunk(rva)
            return None, ('in a chunk that changed: %#x..%#x' % c[:2]) if c else 'padding or unknown code'
        k = bisect.bisect_right(self.keys, rva)
        below = self.keys[k-1] if k else None; above = self.keys[k] if k < len(self.keys) else None
        if below is not None and above is not None and below not in self.contested and above not in self.contested:
            d0 = self.targets[below]-below; d1 = self.targets[above]-above
            if d0 == d1 and above-below <= 0x4000:return rva+d0, 'between'
        return None, 'no reference found'


def load(path):
    with open(path, 'rb') as f:return Map(pickle.load(f))


def main(argv):
    if len(argv) > 4 and argv[1] == 'map':
        record = build(argv[2], argv[3])
        with open(argv[4], 'wb') as f:pickle.dump(record, f)
        m, c, a = record['matched'], record['changed'], record['added']
        print(f"matched {len(m)} chunks ({sum(1 for x in m if x[3] != 'code')} with moved fields), "
              f"changed or gone {len(c)} ({sum(1 for x in c if x[2])} functions), new {len(a)} ({sum(1 for x in a if x[2])} functions), "
              f"anchors out of order {record['moved']}, targets {len(record['targets'])}, contested {len(record['contested'])}, "
              f"values that changed {len(record['values'])}")
    elif len(argv) > 3 and argv[1] == 'lookup':
        m = load(argv[2])
        for text in argv[3:]:
            rva = int(text, 16); new, how = m.where(rva)
            print(f'{rva:#x} -> {new:#x} ({how}, {new-rva:+#x})' if new is not None else f'{rva:#x} -> ? ({how})')
    else:
        print(__doc__)


if __name__ == '__main__':
    main(sys.argv)
