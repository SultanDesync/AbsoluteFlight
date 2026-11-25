"""json_port.py TREE OLD_EXE NEW_EXE MAP VERSION BUILDID [--write]: carry the JSON address manifests to the new exe."""
import sys, os, json, re, hashlib
tree, old_exe, new_exe, map_path, version, buildid = sys.argv[1:7]; write = '--write' in sys.argv
sys.path.insert(0, os.path.join(tree, 'tools'))
import game_port, game_port_apply
import capstone as cs
porter = game_port_apply.Porter(old_exe, new_exe, map_path); old, new = porter.old, porter.new
md = cs.Cs(cs.CS_ARCH_X86, cs.CS_MODE_64)
sha = hashlib.sha256(open(new_exe, 'rb').read()).hexdigest()
problems = []

def where(rva, what):
    target, how = porter.where(rva)
    if target is None:problems.append(f'{what}: {rva:#x} not mapped ({how})')
    return target

def text_line(image, rva):
    ins = next(md.disasm(image.data[rva:rva+16], rva), None)
    return f'{ins.address:#x}: {ins.mnemonic} {ins.op_str}'.rstrip() if ins else None

def lines(items, what):
    """'0x1756b50: movss ...' lines: remap the address; re-disassemble when the old line is what capstone prints."""
    out = []; kept = 0
    for item in items:
        m = re.match(r'^(0x[0-9a-f]+): (.*)$', item)
        if not m:out.append(item); kept += 1; continue
        rva = int(m.group(1), 16); target, _ = porter.where(rva)
        if target is None:out.append(item); kept += 1; continue
        out.append(text_line(new, target) if text_line(old, rva) == item else f'{target:#x}: {m.group(2)}')
    return out, kept

def spaced(raw, like):
    body = raw.hex(' '); return body.upper() if like.upper() == like else body

def dump(path, data, indent):
    with open(path, encoding='utf-8', newline='') as f:original = f.read()
    eol = '\r\n' if '\r\n' in original else '\n'
    body = original.replace('\r\n', '\n'); tail = '\n' if body.endswith('\n') else ''
    for ascii_only in (True, False):
        if json.dumps(json.loads(body), indent=indent, ensure_ascii=ascii_only)+tail == body:break
    else:
        problems.append(f'{path}: formatting does not round-trip; not written'); return
    if write:
        with open(path, 'w', encoding='utf-8', newline='') as f:f.write((json.dumps(data, indent=indent, ensure_ascii=ascii_only)+tail).replace('\n', eol))

def identity(d):
    for key, value in (('file_version', version), ('sha256', sha), ('steam_buildid', buildid)):
        if key in d:d[key] = value

for name in ('2026-10-15_codex_options-ui-static-v4.json', '2026-10-15_codex_ui-range-static.json'):
    path = os.path.join(tree, 'mod', 'data', name)
    with open(path, encoding='utf-8') as f:d = json.load(f)
    identity(d); done = 0
    for key, t in d['targets'].items():
        rva = int(t['rva'], 16); target = where(rva, f'{name} {key}')
        if target is None:continue
        if 'entry32' in t:
            if old.data[rva:rva+32].hex() != t['entry32']:problems.append(f'{name} {key}: old entry32 is not in the old exe'); continue
            t['entry32'] = new.data[target:target+32].hex()
        t['rva'] = f'{target:#x}'; done += 1
        sig = t.get('signature') if isinstance(t.get('signature'), dict) else t if 'pattern' in t else None
        if sig and '?' in sig['pattern']:
            # A wildcard pattern stays as written when the new entry still fits it; its list of matches is remapped.
            tokens = sig['pattern'].split()
            if not all(tok.startswith('?') or new.data[target+i] == int(tok, 16) for i, tok in enumerate(tokens)):
                problems.append(f'{name} {key}: wildcard pattern no longer fits the entry')
            if isinstance(sig.get('matches'), list):
                fresh = []
                for item in sig['matches']:
                    there = where(int(item, 16) if isinstance(item, str) else item, f'{name} {key} match')
                    fresh.append(item if there is None else f'{there:#x}' if isinstance(item, str) else there)
                sig['matches'] = fresh
        elif sig:
            raw = bytes.fromhex(sig['pattern']); at = int(sig['rva'], 16) if 'rva' in sig and sig is not t else rva
            there = where(at, f'{name} {key} signature')
            if there is not None:
                fresh = new.data[there:there+len(raw)]; count = new.data.count(fresh, new.text[0], new.text[1])
                sig['pattern'] = spaced(fresh, sig['pattern'])
                if sig is not t:sig['rva'] = f'{there:#x}'
                for field in ('match_count', 'matches'):
                    if field in sig:
                        if not isinstance(sig[field], list) and sig[field] != count:problems.append(f'{name} {key}: pattern now matches {count} places (was {sig[field]})')
                        if not isinstance(sig[field], list):sig[field] = count
    for key, value in d.get('globals', {}).items():
        if isinstance(value, str) and value.startswith('0x'):
            target = where(int(value, 16), f'{name} global {key}')
            if target is not None:d['globals'][key] = f'{target:#x}'
    for item in d.get('menu_index_instructions', []):
        rva = int(item['rva'], 16); target = where(rva, f'{name} menu index instruction')
        if target is None:continue
        was = text_line(old, rva)
        if was and was.split(': ', 1)[1] == item['instruction']:item['instruction'] = text_line(new, target).split(': ', 1)[1]
        else:problems.append(f'{name}: instruction at {rva:#x} is not what capstone prints; text kept')
        item['rva'] = f'{target:#x}'
    print(f'{name}: {done}/{len(d["targets"])} targets')
    dump(path, d, 1)

path = os.path.join(tree, 'tools', 'native_newtonian_sites.json')
with open(path, encoding='utf-8') as f:d = json.load(f)
identity(d['build'])
for kind, site in d['sites'].items():
    rva = site['rva']; target = where(rva, f'site {kind}')
    if target is None:continue
    for field in ('original', 'signature'):
        raw = bytes.fromhex(site[field])
        if old.data[rva:rva+len(raw)] != raw:problems.append(f'site {kind}: old {field} is not in the old exe'); continue
        fresh = new.data[target:target+len(raw)]
        if field == 'original' and fresh != raw:problems.append(f'site {kind}: ORIGINAL BYTES CHANGED {raw.hex()} -> {fresh.hex()} (the cave runs them)')
        site[field] = fresh.hex()
    site['rva'] = target
    for field in ('instructions', 'context'):
        if field in site:site[field], _ = lines(site[field], kind)
    print(f'site {kind}: {rva:#x} -> {target:#x}')
for field in ('prologue', 'track_context', 'relative_context'):
    d[field], kept = lines(d[field], field)
    if kept:print(f'{field}: {kept} lines kept as they were')
dump(path, d, 2)
print('PROBLEMS:', len(problems))
for p in problems:print('  '+p)
