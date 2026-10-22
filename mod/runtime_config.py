"""Runtime config: tracked config/runtime.json plus an optional per-machine config/runtime.local.json.

The tracked file holds repo-relative defaults that are the same on every machine. The local file
(gitignored) overrides any key for one machine: the built bus DLL's path and its SHA256 pin differ
per build, so each machine pins its own. Objects merge key by key, and any other value replaces.
Relative bus paths resolve against the repository root.

data_dir() is where settings, status and logs go: ABSOLUTE_HOTAS_DATA if set, else "data_dir" from the
config (repo-relative; the tracked file says "config" so dev checkouts keep theirs), else
%LOCALAPPDATA%\\AbsoluteHOTAS for a release install, whose runtime.json leaves it out (T100).
"""
import json
import os
from pathlib import Path

PATH_KEYS=('dll','abi_source')


def merge(base,override):
    out=dict(base)
    for k,v in override.items():out[k]=merge(out[k],v) if isinstance(v,dict) and isinstance(out.get(k),dict) else v
    return out


def load(root):
    root=Path(root).resolve();config=json.loads((root/'config'/'runtime.json').read_text(encoding='utf-8'))
    local=root/'config'/'runtime.local.json'
    if local.exists():config=merge(config,json.loads(local.read_text(encoding='utf-8')))
    bus=config.get('bus')
    if isinstance(bus,dict):
        for k in PATH_KEYS:
            if isinstance(bus.get(k),str) and not Path(bus[k]).is_absolute():bus[k]=str(root/bus[k])
    return config


def data_dir(root,config=None):
    if os.environ.get('ABSOLUTE_HOTAS_DATA'):return Path(os.environ['ABSOLUTE_HOTAS_DATA'])
    root=Path(root).resolve();config=load(root) if config is None else config
    if isinstance(config.get('data_dir'),str):
        data=Path(config['data_dir']);return data if data.is_absolute() else root/data
    return Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData'/'Local')/'AbsoluteHOTAS'
