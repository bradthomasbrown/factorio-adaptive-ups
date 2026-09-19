"""Private, persistent credentials for self-service helper downloads."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import time


class EnrollmentLimit(Exception):
    pass


class AccessStore:
    def __init__(self, path):
        self.path = Path(path)

    def initialize(self):
        self.path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS profiles (
                    digest TEXT PRIMARY KEY, player TEXT NOT NULL,
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (address TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS requests_created ON requests(created);
                CREATE INDEX IF NOT EXISTS requests_address ON requests(address, created);
            ''')
        self.path.chmod(0o640)

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    def issue(self, player, token, address, now=None):
        now = time.time() if now is None else now
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM requests WHERE created < ?', (now - 3600,))
            recent, hourly = db.execute(
                'SELECT SUM(created > ?), COUNT(*) FROM requests WHERE address = ?',
                (now - 60, address)).fetchone()
            total = db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
            profiles = db.execute('SELECT COUNT(*) FROM profiles').fetchone()[0]
            if (recent or 0) >= 6 or hourly >= 20 or total >= 120 or profiles >= 10000:
                raise EnrollmentLimit('Too many access-file requests. Please try again later.')
            db.execute('INSERT INTO requests VALUES (?, ?)', (address, now))
            db.execute('INSERT INTO profiles(digest, player, created) VALUES (?, ?, ?)',
                       (self.digest(token), player, now))

    def authorize(self, player, token):
        if not isinstance(player, str) or not isinstance(token, str) or not 24 <= len(token) <= 256:
            return False
        # Read-only connections never create a missing database or change it.
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                return db.execute('SELECT 1 FROM profiles WHERE digest = ? AND player = ?',
                                  (self.digest(token), player)).fetchone() is not None
        except sqlite3.Error:
            return False

    def revoke(self, player):
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            db.execute('DELETE FROM profiles WHERE player = ?', (player,))
