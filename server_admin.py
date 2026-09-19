"""Root-only operations for the installed server; never prints credentials."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
import zipfile
from adaptive_ups import Rcon
from access_store import AccessStore

CONFIG = Path('/etc/factorio')
DATA = Path('/srv/factorio')

def rcon():
    env = dict(line.split('=', 1) for line in (CONFIG / 'rcon.env').read_text().splitlines() if '=' in line)
    return Rcon('127.0.0.1', 27015, env['FACTORIO_RCON_PASSWORD'], timeout=180)

def atomic_json(path, data, mode=0o640):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n')
    os.chmod(temp, mode)
    shutil.chown(temp, user='root', group='factorio')
    temp.replace(path)

def backup():
    # Every successful backup is both ZIP-verified and a load-latest checkpoint.
    # Runtime saves use a rotating slot; historical backups are outside saves/.
    lock = (DATA / 'backups/.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target = DATA / 'saves/_managed-checkpoint.zip'
    before = target.stat().st_mtime_ns if target.exists() else 0
    r = rcon()
    try:
        response = r.command('/server-save _managed-checkpoint')
        if 'Unknown command' in response:
            raise RuntimeError('Server-save unavailable')
        for _ in range(180):
            if target.exists() and target.stat().st_mtime_ns != before:
                try:
                    with zipfile.ZipFile(target) as archive:
                        if archive.testzip() is not None:
                            raise RuntimeError('Save CRC check failed')
                    break
                except zipfile.BadZipFile:
                    pass
            time.sleep(1)
        else:
            raise TimeoutError('Save did not complete')
    finally:
        r.close()
    dest = DATA / 'backups' / ('checkpoint-' + stamp + '.zip')
    shutil.copy2(target, dest)
    os.chmod(dest, 0o640)
    digest = hashlib.file_digest(dest.open('rb'), 'sha256').hexdigest()
    (dest.with_suffix('.sha256')).write_text(digest + '  ' + dest.name + '\n')
    records = sorted((DATA / 'backups').glob('checkpoint-*.zip'), reverse=True)
    # Keep 24 recent snapshots plus one per day for the preceding 14 days.
    keep = set(records[:24])
    days = set()
    for item in records:
        day = item.name[11:19]
        if day not in days and len(days) < 14:
            keep.add(item)
            days.add(day)
    for item in records:
        if item not in keep:
            item.unlink()
            item.with_suffix('.sha256').unlink(missing_ok=True)
    print(json.dumps({'backup': str(dest), 'sha256': digest, 'bytes': dest.stat().st_size}))

def enroll(player):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', player):
        raise ValueError('Use the exact Factorio account name (letters, numbers, underscore, dot or hyphen)')
    config = json.loads((CONFIG / 'adaptive.json').read_text())
    if config.get('access_database') and Path(config['access_database']).exists():
        AccessStore(config['access_database']).revoke(player)
    config['client_tokens'][player] = secrets.token_urlsafe(32)
    atomic_json(CONFIG / 'adaptive.json', config)
    profiles = CONFIG / 'profiles'
    profiles.mkdir(mode=0o700, exist_ok=True)
    profile = {'version': 1, 'server': 'https://94.16.31.89',
               'player': player, 'token': config['client_tokens'][player]}
    atomic_json(profiles / (player + '.json'), profile, 0o600)
    subprocess.run(['systemctl', 'restart', 'adaptive-ups'], check=True)
    print('Helper credentials rotated. Private replacement file: ' + str(profiles / (player + '.json')))

def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='action', required=True)
    for name in ['status', 'backup', 'adaptive']:
        commands.add_parser(name)
    commands.add_parser('fixed').add_argument('ups', type=float)
    commands.add_parser('enroll').add_argument('player')
    args = parser.parse_args()
    if args.action == 'backup':
        backup()
    elif args.action == 'enroll':
        enroll(args.player)
    elif args.action == 'adaptive':
        subprocess.run(['systemctl', 'restart', 'adaptive-ups'], check=True)
        print('Adaptive control restarted at fallback speed.')
    else:
        if args.action == 'fixed':
            if not 0.6 <= args.ups <= 60:
                raise ValueError('UPS must be between 0.6 and 60')
            subprocess.run(['systemctl', 'stop', 'adaptive-ups'], check=True)
        r = rcon()
        try:
            if args.action == 'fixed':
                r.json_command(f'/adaptive-ups-enable {secrets.token_hex(16)} {args.ups:g}')
                r.json_command('/adaptive-ups-disable')
            status = r.json_command('/adaptive-ups-status')
            status.pop('owner', None)
            print(json.dumps(status, indent=2))
        finally:
            r.close()

if __name__ == '__main__':
    main()
