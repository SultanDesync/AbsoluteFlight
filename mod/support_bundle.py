"""Support bundle (T100): one zip a player attaches to a bug report, saved from the Absolute Flight menu.

Holds settings, status, the flight tunings, the runtime config, the last two sessions' runtime and key logs, the startup
logs (proxy.log, python-boot.log and the last two pyMHF logs, D22), the newest telemetry CSV when it is small enough,
and an info.json the caller fills (game build, versions). Goes to
<data>/support/. Only reads files; nothing is sent anywhere.
"""
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SESSIONS = 2
TELEMETRY_LIMIT = 64 * 1024 * 1024


def _newest(logs, pattern, count):
    return sorted(logs.glob(pattern), key=lambda p: p.name, reverse=True)[:count]


def contents(data, config_dir=None):
    """(archive name, path) pairs for the bundle, newest sessions first."""
    data = Path(data); logs = data / 'logs'; files = []
    for name in ('hotas-settings.json', 'hotas-status.json'):
        if (data / name).is_file(): files.append((name, data / name))
    tunings = data / 'flight-tunings'
    if tunings.is_dir():
        files.extend(('flight-tunings/' + p.name, p) for p in sorted(tunings.glob('*.json')))
    if config_dir is not None:
        for name in ('runtime.json', 'runtime.local.json'):
            if (Path(config_dir) / name).is_file(): files.append(('config/' + name, Path(config_dir) / name))
    if logs.is_dir():
        for runtime in _newest(logs, '*-runtime.log', SESSIONS):
            stamp = runtime.name[:-len('-runtime.log')]
            for log in (runtime, logs / (stamp + '-keys.log')):
                if log.is_file(): files.append(('logs/' + log.name, log))
        # How the game-folder install started: the proxy, Python, then pyMHF's hooks (D22).
        startup = [logs / 'proxy.log', logs / 'python-boot.log', *_newest(logs, 'pymhf-*.log', SESSIONS)]
        files.extend(('logs/' + log.name, log) for log in startup if log.is_file())
        for telemetry in _newest(logs, '*-telemetry.csv', 1):
            if telemetry.stat().st_size <= TELEMETRY_LIMIT: files.append(('logs/' + telemetry.name, telemetry))
    return files


def save(data, info, config_dir=None, now=None):
    """Write the zip and return its path."""
    now = now or datetime.now(timezone.utc)
    folder = Path(data) / 'support'; folder.mkdir(parents=True, exist_ok=True)
    path = folder / ('support-' + now.strftime('%Y%m%dT%H%M%SZ') + '.zip')
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('info.json', json.dumps({'saved': now.isoformat(), **info}, indent=1))
        for name, source in contents(data, config_dir):
            try: archive.write(source, name)
            except OSError as error: archive.writestr(name + '.error.txt', repr(error))  # a log still open for writing
    return path
