"""Start Absolute Flight inside No Man's Sky, run by the winmm.dll proxy (D22, T100).

The proxy loads this folder's python311.dll into the game on a thread of its own, sets AF_HOME and runs
this file in __main__. It does in-process what `pymhf run nmspy` (play.py) does from outside: build
the same config, set pyMHF's internals, then run pyMHF's injected.py, which loads the mods and serves
until the game exits. Any game build other than the release mod's stays vanilla: nothing is hooked.
"""
import os
import os.path as op
import re
import sys
import time

AF_HOME = globals()['AF_HOME']  # set by the proxy before this file runs
DATA = op.join(os.environ.get('LOCALAPPDATA') or op.expanduser('~'), 'AbsoluteHOTAS')
LOGS = op.join(DATA, 'logs')
os.makedirs(LOGS, exist_ok=True)
# The game has no console: Python's own output goes to a file.
sys.stdout = sys.stderr = open(op.join(LOGS, 'python-boot.log'), 'a', encoding='utf-8', buffering=1)
print(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} Absolute Flight {open(op.join(AF_HOME, 'VERSION')).read().strip()} "
      f"starting in-process from {AF_HOME} (Python {sys.version.split()[0]})")


def af_start():
    import ctypes
    exe = ctypes.create_unicode_buffer(32768)
    ctypes.windll.kernel32.GetModuleFileNameW(None, exe, len(exe))
    binary_path, binary_exe = exe.value, op.basename(exe.value)

    # pyMHF builds questionary prompts on import; give them a dummy terminal (as tools/launch_options_probe.py does).
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput
    with create_app_session(input=DummyInput(), output=DummyOutput()):
        import pymhf  # noqa: F401
        import pymhf.main as pm
    import importlib.resources as impres
    from importlib.metadata import entry_points

    import pymem

    with open(binary_path, 'rb') as f:
        binary_hash = pm.hash_bytes_from_file(f)
    mod_dir = op.join(AF_HOME, 'mod', 'release')
    with open(op.join(mod_dir, 'absolute_hotas.py'), encoding='utf-8') as f:
        wanted = re.search(r"^BUILD_HASH\s*=\s*'([0-9a-f]+)'", f.read(), re.M).group(1)
    if binary_hash != wanted:
        print(f'game build hash {binary_hash} is not the one this release supports ({wanted}): staying off')
        return

    # load_module('nmspy', ...): the library's pymhf.toml, then what play.py would put in pymhf.local.toml.
    plugin_name = 'nmspy'
    module_path = pm.library_path_from_name(plugin_name)
    config = pm.read_pymhf_settings(op.join(module_path, 'pymhf.toml'))
    config.update({'mod_dir': mod_dir, 'gui': {'shown': False}, 'start_exe': False, 'interactive_console': False,
                   'logging': {'shown': False, 'log_level': 'info', 'log_dir': LOGS}})
    # run_module: merge every pyMHF library's own config underneath.
    final = {}
    for library in [ep.name for ep in entry_points().select(group='pymhflib')]:
        if impres.is_resource(library, 'pymhf.toml'):
            with impres.path(library, 'pymhf.toml') as toml:
                lib_config = pm.read_pymhf_settings(toml)
                if (library_path := pm.library_path_from_name(library)) is not None:
                    pm.canonicalize_settings_inline(lib_config, library, library_path, None, None, ['{CURR_DIR}', '{USER_DIR}'])
                pm.merge_configs(lib_config, final)
    pm.merge_configs(config, final)
    config = final
    binary_dir = op.dirname(binary_path)
    mod_save_dir = pm.canonicalize_setting(config.get('mod_save_dir', '{CURR_DIR}'), plugin_name, module_path, binary_dir, 'MOD_SAVES')

    # sys.path as run_module sets it for a library: pyMHF's parent, the library's parent, the mod folder
    # and each of its subfolders holding a mod.
    for path in (pm.PYMHF_DIR, op.dirname(module_path), mod_dir):
        if path not in sys.path: sys.path.insert(0, path)
    for name in os.listdir(mod_dir):
        folder = op.join(mod_dir, name)
        if op.isdir(folder) and folder not in sys.path:
            for pyfile in pm.glob.iglob(op.join(folder, '*.py')):
                with open(pyfile, 'r') as f:
                    if pm.parse_file_for_mod(f.read()):
                        sys.path.insert(0, folder); break

    # pymem names a module it couldn't read (base None) after the exe, since GetModuleBaseName(NULL) returns
    # the exe's name, so injected.py's {name: module} map loses NMS.exe's real base and every hook fails.
    # In-process, the game is still loading and unloading DLLs while that list is built (2026-10-24: 0 hooks).
    import pymem.process
    enum_modules = pymem.process.enum_process_module
    pymem.process.enum_process_module = lambda handle: (m for m in enum_modules(handle) if m.lpBaseOfDll)

    pm_binary = pymem.Pymem(os.getpid())
    import pymhf.core._internal as internal
    internal.MODULE_PATH = module_path
    internal.BASE_ADDRESS = pm_binary.process_base.lpBaseOfDll
    internal.SIZE_OF_IMAGE = pm_binary.process_base.SizeOfImage
    internal.CWD = pm.CWD
    internal.PID = os.getpid()
    internal.HANDLE = pm_binary.process_handle
    internal.BINARY_HASH = binary_hash
    internal.CONFIG = config
    internal.EXE_NAME = binary_exe
    internal.BINARY_PATH = binary_path
    internal.LOAD_TYPE = pm.LoadTypeEnum.LIBRARY.value
    internal.MOD_SAVE_DIR = mod_save_dir
    internal.INCLUDED_ASSEMBLIES = {}
    internal.CACHE_DIR = op.join(os.environ['APPDATA'], 'pymhf', plugin_name, '.cache')
    internal._SENTINEL_PTR = 0
    print(f'pyMHF internals set (base {internal.BASE_ADDRESS:#x}); running injected.py, logs in {LOGS}')
    return op.join(pm.CWD, 'injected.py')


try:
    af_injected = af_start()
except Exception:
    import traceback
    print('Absolute Flight could not start; the game plays without it:')
    traceback.print_exc()
    af_injected = None
if af_injected:
    exec(compile(open(af_injected, 'rb').read(), af_injected, 'exec'))  # in __main__, as pyMHF runs it
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} pyMHF stopped")
