"""Build the deterministic mod archive; optionally package the Windows companion."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import zipfile

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--exe', action='store_true')
args = parser.parse_args()
source = root / 'mod/adaptive-ups_0.2.1'
info = json.loads((source / 'info.json').read_text(encoding='utf-8'))
assert source.name == f"{info['name']}_{info['version']}"
resources = root / 'resources'
resources.mkdir(exist_ok=True)
with zipfile.ZipFile(resources / (source.name + '.zip'), 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(source.rglob('*')):
        if path.is_file():
            member = zipfile.ZipInfo(str(path.relative_to(source.parent)).replace('\\', '/'), (2026, 9, 19, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = 0o644 << 16
            archive.writestr(member, path.read_bytes().replace(b'\r\n', b'\n'))
print('Built', source.name + '.zip')
if args.exe:
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
        '--name', 'AdaptiveUPS', '--add-data', str(resources) + ':resources', str(root / 'player_helper.py')], cwd=root, check=True)
