"""VR test report (T125): what one session's log, telemetry and status say about the mod in a headset.

User 2026-11-13: "let's build a test regime and export package function for people who want vr. I cannot continue
testing vr., but we can get telemetry from testers." The tester flies the checklist in VR-TESTING.md and saves a
VR test report from the Absolute Flight menu. The zip is the support bundle plus vr-report.json and
vr-report.txt, written by summarise() here. tools/vr_test_report.py prints the same report from a tester's zip or a
data folder.

Read-only and offline: plain text in, a dict out. Each check is PASS, FAIL, NOT TESTED or INFO with the numbers it
was judged on, so a report can be read without the raw files and argued with from them.
"""
import csv
import io
import json
import re
import zipfile
from pathlib import Path

PASS, FAIL, NOT_TESTED, INFO = 'PASS', 'FAIL', 'NOT TESTED', 'INFO'
MIN_ROWS = 200            # about 4 s of telemetry at 50 Hz: less than this of anything is not a test
CHANNELS = ('pitch', 'yaw', 'roll', 'throttle', 'strafe-lateral', 'strafe-vertical')
MOVED = .05               # an output beyond this counts as the tester using that axis
# Game actions the Buttons page presses (mod/hotas_buttons.py NATIVE_FOR, T125 recentre), for readable reports.
ACTIONS = {0x07: 'Interact', 0x17: 'Quick menu', 0x1B: 'Thrust / launch', 0x1C: 'Brake', 0x1D: 'Boost', 0x1E: 'Roll left',
           0x1F: 'Roll right', 0x20: 'Exit ship', 0x21: 'Land', 0x22: 'Fire weapon', 0x23: 'Change weapon', 0x24: 'Scan',
           0x25: 'Pulse drive', 0x26: 'Galactic map', 0x29: 'Free look', 0x2A: 'Combat follow (toggle)',
           0x2B: 'Combat follow (hold)', 0x2C: 'Cycle power', 0x51: 'Quick menu left', 0x52: 'Quick menu right',
           0x53: 'Quick menu select', 0x55: 'Quick menu up', 0x56: 'Quick menu down', 0x81: 'Next target',
           0x82: 'Previous target', 0x83: 'Closest target', 0xF5: 'Recentre VR view', 0xF6: 'Recentre VR view (2)'}
_HANDS = re.compile(r'VR hands: states (\d+), (\d+); mode (\d+)')
_SET = re.compile(r'game action set (\d+)')
_TROUBLE = re.compile(r'\b(?:failed|error|refused)\b|Traceback|disabled after', re.I)
_ROUTINE = ('hooks failed to install: none',)


def _number(text):
    try:return float(text)
    except (TypeError, ValueError):return None


def _check(result, summary, **detail):
    return {'result': result, 'summary': summary, **detail}


def summarise(log_text='', telemetry=(), status=None, info=None):
    """log_text: the session's runtime log. telemetry: its rows as dicts (csv.DictReader). status: hotas-status.json."""
    status = status or {}; lines = [line for line in (log_text or '').splitlines() if line.strip()]
    def said(text):return [line for line in lines if text in line]
    vr_block = status.get('vr') or {}
    rows = {'0': 0, '1': 0, '2': 0}; armed = conflict_armed = applied = menu = 0; speed = 0.
    moved = {c: 0 for c in CHANNELS}; reasons = {}
    for row in telemetry:
        vr = str(row.get('vr') or '0'); rows[vr] = rows.get(vr, 0)+1
        if vr != '2':continue
        reason = row.get('reason') or ''; reasons[reason] = reasons.get(reason, 0)+1
        if reason in ('menu open', 'game menu'):menu += 1
        if reason != 'armed':continue
        armed += 1
        if str(row.get('conflict')) not in ('0', '', 'None'):conflict_armed += 1
        if str(row.get('applied_mask')) not in ('0', '', 'None'):applied += 1
        speed = max(speed, abs(_number(row.get('forward_speed')) or 0.))
        for channel in CHANNELS:
            value = _number(row.get('out_'+channel))
            if value is not None and abs(value) > MOVED:moved[channel] += 1
    checks = {}
    # 1. Did the mod see the headset?
    displays = [line.split('display: ', 1)[1] for line in said('display: ')]
    seen_vr = any(d.startswith('VR') for d in displays) or vr_block.get('display') == 'vr' or rows['1']+rows['2'] > 0
    checks['detection'] = _check(PASS if seen_vr else FAIL if lines or rows['0'] else NOT_TESTED,
                                 'the mod saw a VR display' if seen_vr else 'no VR display seen: was the game started in VR?',
                                 log=displays[:6], mode=vr_block.get('mode'), pinned=vr_block.get('pinned'),
                                 rows_flat=rows['0'], rows_vr_setting_off=rows['1'], rows_vr_setting_on=rows['2'])
    # 2. Experimental VR switched on
    on = rows['2'] > 0 or any('Experimental VR on' in d for d in displays) or vr_block.get('experimental') is True
    checks['setting'] = _check(PASS if on else NOT_TESTED if not seen_vr else FAIL,
                               'Experimental VR was on' if on else 'Experimental VR was never on (Flight Model page)')
    # 3. The input switch
    switched = said('VR input: the game takes its flat-screen'); broken = said('VR input switch failed')+said('VR input restore')
    fire = 'switched' if any('fire no longer waits' in line for line in switched) else vr_block.get('fire')
    checks['input_switch'] = _check(FAIL if any('failed' in line or 'incomplete' in line for line in broken) else PASS if switched else NOT_TESTED,
                                    'the game was put on its flat-screen stick and throttle' if switched else 'the input switch never went in',
                                    fire=fire, flat_input=vr_block.get('flat_input'),
                                    flat_input_switches=len(said('VR flat-screen input in flight: on')),
                                    flat_input_refused=[line[33:] for line in said('VR flat-screen input left native')][:2],
                                    log=[line[33:] if len(line) > 33 else line for line in (switched+broken)[:4]])
    # 4. Flight on the HOTAS
    in_flight = rows['2']-menu
    if rows['2'] < MIN_ROWS:flight = _check(NOT_TESTED, 'not enough flight with Experimental VR on')
    elif armed < MIN_ROWS or armed < .5*in_flight:
        flight = _check(FAIL, 'the HOTAS was not flying the ship for most of the flight')
    elif conflict_armed > .05*armed or applied < .5*armed:
        flight = _check(FAIL, 'armed, but the game refused or ignored the HOTAS input')
    else:flight = _check(PASS, 'the HOTAS flew the ship')
    flight.update(rows=rows['2'], armed=armed, conflict_while_armed=conflict_armed, applied=applied, menu=menu,
                  max_forward_speed=round(speed, 1), reasons=dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:6]))
    checks['flight'] = flight
    # 5. Which axes the tester actually used
    used = [c for c in CHANNELS if moved[c] >= 25]
    checks['axes_used'] = _check(INFO if armed >= MIN_ROWS else NOT_TESTED,
                                 'moved: '+(', '.join(used) or 'none')+('; not moved: '+', '.join(c for c in CHANNELS if c not in used) if len(used) < len(CHANNELS) else ''),
                                 rows_moved=moved)
    # 6. Buttons: did the game ask for what was held
    native = ((status.get('key_output') or {}).get('native') or {})
    asked = {int(k, 16): int(v) for k, v in (native.get('asked') or {}).items()}
    named = {ACTIONS.get(a, f'action {a:#x}'): n for a, n in sorted(asked.items())}
    silent = [name for name, n in named.items() if not n]
    if 'asked' not in native:buttons = _check(NOT_TESTED, 'this build does not record what the game asked for')
    elif not asked:buttons = _check(NOT_TESTED, 'no game-action button was held')
    elif native.get('misses') or silent:
        buttons = _check(FAIL, 'held but never asked for by the game: '+', '.join(silent) if silent else 'the game asked on another input port')
    else:buttons = _check(PASS, 'the game asked for every held action (whether it then acted is for the tester to say)')
    buttons.update(asked=named, answered=native.get('hits'), other_port=native.get('misses'), miss_port=native.get('miss_port'),
                   miss_active=native.get('miss_active'), any_port=native.get('any_port'),
                   path=(status.get('key_output') or {}).get('path'))
    checks['buttons'] = buttons
    # 7. The game's VR hand-control fields (what its fire test reads)
    hands = []
    for line in lines:
        match = _HANDS.search(line)
        if match and match.groups() not in hands:hands.append(match.groups())
    checks['hands'] = _check(INFO if hands else NOT_TESTED, f'{len(hands)} distinct hand states seen',
                             states=[{'left': int(a), 'right': int(b), 'mode': int(c)} for a, b, c in hands[:20]],
                             last=vr_block.get('hands'))
    # 8. Anything that went wrong
    trouble = [line[33:] if len(line) > 33 else line for line in lines if _TROUBLE.search(line) and not any(r in line for r in _ROUTINE)]
    conflicts = len(said('native input conflict')); unfocused = len(said('game not focused'))
    errors = int(status.get('errors') or 0)+int(status.get('hook_errors') or 0)
    checks['errors'] = _check(FAIL if errors or trouble else PASS, f'{errors} runtime errors, {len(trouble)} log lines of trouble',
                              lines=trouble[:12], input_conflict_lines=conflicts, not_focused_lines=unfocused,
                              watchdog_resets=(status.get('watchdog') or {}).get('resets'))
    checks['action_sets'] = _check(INFO, 'game action sets seen', sets=sorted({int(m.group(1)) for line in lines for m in [_SET.search(line)] if m}))
    return {'schema': 1, 'info': info or {}, 'checks': checks}


def text(report):
    """The report as lines a person can read in Discord or a terminal."""
    out = ['Absolute Flight VR test report']
    for key, value in (report.get('info') or {}).items():out.append(f'  {key}: {value}')
    for name, check in report['checks'].items():
        out.append(f"{check['result']:<10} {name}: {check['summary']}")
        for key, value in check.items():
            if key in ('result', 'summary') or value in (None, [], {}, ''):continue
            out.append(f'             {key}: {json.dumps(value) if isinstance(value, (dict, list)) else value}')
    return '\n'.join(out)+'\n'


def _session(names):
    """The newest runtime log's stamp among archive or folder names."""
    stamps = sorted(n[:-len('-runtime.log')] for n in names if n.endswith('-runtime.log'))
    return stamps[-1] if stamps else None


def from_folder(data, info=None):
    """The newest session in a data folder (%LOCALAPPDATA%\\AbsoluteHOTAS)."""
    data = Path(data); logs = data/'logs'
    stamp = _session([p.name for p in logs.glob('*-runtime.log')]) if logs.is_dir() else None
    log = (logs/(stamp+'-runtime.log')).read_text(encoding='utf-8', errors='replace') if stamp else ''
    status = {}
    try:status = json.loads((data/'hotas-status.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):pass
    telemetry = logs/(stamp+'-telemetry.csv') if stamp else None
    if telemetry is not None and telemetry.is_file():
        with telemetry.open(newline='', encoding='utf-8', errors='replace') as handle:
            return summarise(log, csv.DictReader(handle), status, {**(info or {}), 'session': stamp})
    return summarise(log, (), status, {**(info or {}), 'session': stamp})


def from_zip(path):
    """A tester's saved zip (support bundle layout: logs/, hotas-status.json, info.json)."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist(); stamp = _session([n.split('/')[-1] for n in names])
        def read(name):return archive.read(name).decode('utf-8', errors='replace') if name in names else ''
        log = read(f'logs/{stamp}-runtime.log') if stamp else ''
        status, info = {}, {}
        try:status = json.loads(read('hotas-status.json') or '{}')
        except ValueError:pass
        try:info = json.loads(read('info.json') or '{}')
        except ValueError:pass
        rows = csv.DictReader(io.StringIO(read(f'logs/{stamp}-telemetry.csv'))) if stamp else ()
        keep = {k: info[k] for k in ('saved', 'version', 'game_build', 'game_build_hash', 'flight_model') if k in info}
        return summarise(log, rows, status, {**keep, 'session': stamp})
