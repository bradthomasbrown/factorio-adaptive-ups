"""Start Factorio and keep its RCON password out of the service journal."""
import os
import signal
import subprocess
import sys

password = os.environ['FACTORIO_RCON_PASSWORD']
args = ['/opt/factorio/bin/x64/factorio', '--config', '/etc/factorio/config.ini',
        '--mod-directory', '/srv/factorio/mods', '--start-server-load-latest',
        '--server-settings', '/etc/factorio/server-settings.json',
        '--server-adminlist', '/srv/factorio/server-adminlist.json',
        '--bind', '0.0.0.0:34197', '--rcon-bind', '127.0.0.1:27015',
        '--rcon-password', password]
child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding='utf-8', errors='replace')
def stop(signum, frame):
    if child.poll() is None:
        child.send_signal(signal.SIGINT)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
for line in child.stdout:
    sys.stdout.write(line.replace(password, '[redacted]'))
    sys.stdout.flush()
raise SystemExit(child.wait())
