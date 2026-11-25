"""repin.py OLD_TREE NEW_TREE OLD_EXE NEW_EXE MAP [--write]: recompute the sha256 pins over code ranges."""
import sys, os, hashlib, importlib.util, re
old_tree, new_tree, old_exe, new_exe, map_path = sys.argv[1:6]; write = '--write' in sys.argv
sys.path.insert(0, os.path.join(new_tree, 'tools')); sys.path.insert(0, os.path.join(new_tree, 'mod'))
import game_port, game_port_apply
porter = game_port_apply.Porter(old_exe, new_exe, map_path)
old, new = porter.old.data, porter.new.data

def load(tree, name, tag):
    spec = importlib.util.spec_from_file_location(tag+'_'+name, os.path.join(tree, 'tools', name+'.py'))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def digest(data, ranges):
    h = hashlib.sha256()
    for rva, size in ranges:h.update(data[rva:rva+size])
    return h.hexdigest()

jobs = [('native_actions', 'PIN_SHA256', lambda m: m.PIN_RANGES), ('native_analog_actions', 'PIN_SHA256', lambda m: m.PIN_RANGES),
        ('native_crash_damage', 'PIN_SHA256', lambda m: m.PIN_RANGES), ('native_rebind', 'PIN_SHA256', lambda m: m.PIN_RANGES),
        ('native_eject', 'BLOCK_SHA256', lambda m: ((m.BLOCK_RVA, m.BLOCK_END-m.BLOCK_RVA),))]
for name, constant, ranges in jobs:
    a = load(old_tree, name, 'old'); b = load(new_tree, name, 'new')
    ra, rb = tuple(ranges(a)), tuple(ranges(b)); was = getattr(a, constant)
    ok_old = digest(old, ra) == was
    lined = True
    for (o, size), (n, size2) in zip(ra, rb):
        first, _ = porter.where(o); last, _ = porter.where(o+size-1)
        if size != size2 or first != n or last != n+size-1:
            lined = False; print(f'  {name}: range {o:#x}+{size:#x} -> {n:#x}+{size2:#x} does not line up (first {first}, last {last})')
    fresh = digest(new, rb)
    print(f'{name}.{constant}: old pin reproduces {ok_old}, ranges line up {lined}, new {fresh[:16]}...')
    if write and ok_old and lined:
        path = os.path.join(new_tree, 'tools', name+'.py')
        with open(path, encoding='utf-8', newline='') as f:text = f.read()
        if text.count(was) != 1:print('  constant not found once'); continue
        with open(path, 'w', encoding='utf-8', newline='') as f:f.write(text.replace(was, fresh))
        print('  written')
