"""Start No Man's Sky with Absolute Flight (alpha).

Points pyMHF's NMS.py config at this folder's mod\\release, then runs pyMHF the way the dev install does
(`pymhf run nmspy`): Steam starts the game paused, pyMHF loads Python into it, then the game resumes.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def point_pymhf_here():
    from pymhf.utils.parse_toml import read_pymhf_settings, write_pymhf_settings
    folder = Path(os.environ['APPDATA']) / 'pymhf' / 'nmspy'; folder.mkdir(parents=True, exist_ok=True)
    config = folder / 'pymhf.local.toml'
    settings = read_pymhf_settings(config) if config.exists() else {}
    local = dict(settings.get('local_config', {}))
    mod_dir = str(HERE / 'mod' / 'release')
    if local.get('mod_dir') != mod_dir:
        if local.get('mod_dir'): print(f"pyMHF mod folder was {local['mod_dir']}; now {mod_dir}")
        local['mod_dir'] = mod_dir
        write_pymhf_settings({**settings, 'local_config': local}, config)
    (folder / '.config_in_progress').unlink(missing_ok=True)  # an unfinished pyMHF setup would ask questions


def main():
    print(f"Absolute Flight {(HERE / 'VERSION').read_text().strip()}")
    point_pymhf_here()
    import pymhf
    sys.argv = ['pymhf', 'run', 'nmspy']
    pymhf.run()


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print(f'\nAbsolute Flight could not start: {error!r}')
        input('Press Enter to close.')
        raise SystemExit(1)
