"""verify_all.py TREE EXE: run every module-level verify(read, base) and check every {rva: bytes} dict against the exe."""
import sys, os, glob, importlib.util, inspect
tree, exe = sys.argv[1:3]
sys.path.insert(0, os.path.join(tree, 'tools')); sys.path.insert(0, os.path.join(tree, 'mod'))
import game_port
image = game_port.Image(exe); data = image.data
read = lambda address, size: data[address:address+size]
ok = bad = 0; failures = []; skipped = []
for path in sorted(glob.glob(os.path.join(tree, 'mod', '*.py'))+glob.glob(os.path.join(tree, 'tools', '*.py'))):
    name = os.path.splitext(os.path.basename(path))[0]
    if name.startswith('game_port'):continue
    try:
        spec = importlib.util.spec_from_file_location('check_'+name, path); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    except BaseException as error:
        skipped.append(f'{name}: {type(error).__name__}: {str(error)[:80]}'); continue
    for attr, value in vars(m).items():
        if isinstance(value, dict) and value and all(isinstance(k, int) and 0x1000 <= k < image.size for k in value):
            for rva, raw in value.items():
                if isinstance(raw, tuple) and all(isinstance(x, str) for x in raw):raw = bytes.fromhex(''.join(raw))
                elif isinstance(raw, str):
                    try:raw = bytes.fromhex(raw)
                    except ValueError:continue
                if not isinstance(raw, (bytes, bytearray)):continue
                if read(rva, len(raw)) == raw:ok += 1
                else:bad += 1; failures.append(f'{name}.{attr}[{rva:#x}]')
        elif inspect.isfunction(value) and attr.startswith('verify') and value.__module__ == m.__name__:
            params = list(inspect.signature(value).parameters)
            if params[:2] == ['read', 'base']:
                try:result = value(read, 0)
                except BaseException as error:result = f'{type(error).__name__}: {error}'
                if result is True:ok += 1
                else:bad += 1; failures.append(f'{name}.{attr}() -> {result}')
print(f'{ok} checks pass, {bad} fail; {len(skipped)} modules not importable here')
for f in failures:print('  FAIL', f)
for s in skipped:print('  skipped', s)
