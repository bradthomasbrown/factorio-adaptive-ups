"""Adaptive UPS companion. Standard-library only; never launches Factorio."""
from __future__ import annotations
import argparse
import http.client
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse

VERSION = "0.3.0"
SETUP_URL = "https://94.16.31.89"
GAME_ADDRESS = "94.16.31.89:34197"
MOD_FILE = "adaptive-ups_0.2.1.zip"
PLAYER = re.compile(r"[A-Za-z0-9_.-]{1,64}")
INVALID_ACCESS_FILE = "This is not an Adaptive UPS profile. Ordinary setup obtains access automatically; Factorio's player-data.json is not a helper profile."
EXPLANATION = (
    "Every player simulates the whole factory. If your computer processes fewer simulation steps "
    "per second than the server, unprocessed steps pile up: you fall behind. The mod writes a small "
    "marker when your simulation reaches a server checkpoint. This companion reads that file and "
    "acknowledges the marker over HTTPS. The server times the round trip and slows down if reports "
    "become late. This estimates delay; it does not read Factorio's exact buffer counter.\n\n"
    "Factorio mods cannot make ordinary HTTPS requests. Their Lua code runs inside the shared "
    "simulation, so an external program reports when YOUR computer reaches each marker. The mod "
    "API has optional localhost UDP, but that would still need a companion and a special game flag. "
    "This file-based approach needs no launch flags.\n\n"
    "The companion extracts only your account name from player-data.json and reads the mod's beacon.json. "
    "It sends your name, marker session and sequence number, plus its own reporting credential. "
    "Your Factorio sign-in token is not sent. Keep the companion open while playing."
)

def validate_server(server, allow_local=False):
    if not isinstance(server, str):
        raise ValueError("A secure HTTPS server address is required.")
    p = urlparse(server)
    local = allow_local and p.scheme == "http" and p.hostname in {"localhost", "127.0.0.1", "::1"}
    if (p.scheme != "https" and not local) or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in {"", "/"}:
        raise ValueError("A secure HTTPS server address without credentials or a path is required.")
    return p

def validate_profile(value, allow_local=False):
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError(INVALID_ACCESS_FILE)
    if not isinstance(value.get("player"), str) or not PLAYER.fullmatch(value["player"]):
        raise ValueError(INVALID_ACCESS_FILE)
    token = value.get("token")
    if not isinstance(token, str) or not 24 <= len(token) <= 256 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError(INVALID_ACCESS_FILE)
    validate_server(value.get("server"), allow_local)
    return dict(value)

def read_json(path, maximum=65536):
    with Path(path).open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("File is too large.")
    return json.loads(raw.decode("utf-8-sig"))

def load_profile(path):
    if Path(path).name.lower() == "player-data.json":
        raise ValueError(INVALID_ACCESS_FILE)
    return validate_profile(read_json(path))

def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)
    os.replace(temporary, path)

def detect_player(directory):
    try:
        name = read_json(Path(directory) / "player-data.json", 4 * 1024 * 1024).get("service-username")
        return name if isinstance(name, str) and PLAYER.fullmatch(name) else ""
    except (OSError, ValueError, AttributeError):
        return ""

def find_data_directories(home=None, roaming=None):
    home = Path(home) if home is not None else Path.home()
    roaming = os.environ.get("APPDATA") if roaming is None else roaming
    candidates = [Path(roaming) / "Factorio"] if roaming else []
    candidates.extend(home.glob("factorio*/Factorio*"))
    candidates.extend(home.glob("Factorio*"))
    unique = {}
    for path in candidates:
        if path.is_dir() and any((path / n).exists() for n in ("script-output", "mods", "player-data.json")):
            unique[os.path.normcase(str(path.resolve()))] = str(path.resolve())
    return sorted(unique.values())

def find_data_directory():
    candidates = find_data_directories()
    return candidates[0] if len(candidates) == 1 else ""

class ServerResponse(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Server returned HTTP {code}.")

class Connection:
    """One connection per worker. TLS is verified; credentials never redirect."""
    def __init__(self, server, allow_local=False):
        self.address = validate_server(server, allow_local)
        self.conn = None

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def request(self, method, path, value=None, token=None):
        if self.conn is None:
            cls = http.client.HTTPSConnection if self.address.scheme == "https" else http.client.HTTPConnection
            self.conn = cls(self.address.hostname, self.address.port, timeout=2)
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if path == "/join":
            headers["Origin"] = f"{self.address.scheme}://{self.address.netloc}"
        body = None
        if value is not None:
            body = json.dumps(value).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            self.conn.request(method, path, body=body, headers=headers)
            response = self.conn.getresponse()
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("Server response is too large.")
            if response.status not in {200, 201, 204}:
                raise ServerResponse(response.status)
            return json.loads(raw) if raw else None
        except Exception:
            self.close()
            raise

def get_or_create_profile(path, player, server=SETUP_URL, *, allow_local=False, renew=False):
    if not PLAYER.fullmatch(player):
        raise ValueError("Enter your Factorio account name: letters, numbers, underscore, dot or hyphen.")
    if not renew:
        try:
            profile = validate_profile(read_json(path), allow_local)
            if profile["player"] == player and profile["server"].rstrip("/") == server.rstrip("/"):
                return profile
        except (OSError, ValueError):
            pass
    conn = Connection(server, allow_local)
    try:
        profile = validate_profile(conn.request("POST", "/join", {"player": player}), allow_local)
        if profile["player"] != player or profile["server"].rstrip("/") != server.rstrip("/"):
            raise ValueError("The server returned a profile for a different player or server.")
        # Only the UI persists it, after checking this is still the chosen account.
        return profile
    finally:
        conn.close()

def install_mod(source, destination):
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise ValueError("The companion's bundled mod is missing.")
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / MOD_FILE
    if target.exists():
        if target.read_bytes() == source.read_bytes():
            return target
        shutil.copy2(target, target.with_name(target.name + f".backup-{time.time_ns()}"))
    temp = target.with_suffix(".zip.tmp")
    shutil.copy2(source, temp)
    os.replace(temp, target)
    return target

def watch_progress(profile, data_directory, stop, report, *, allow_local=False):
    profile = validate_profile(profile, allow_local)
    path = Path(data_directory) / "script-output/adaptive-ups/beacon.json"
    conn = Connection(profile["server"], allow_local)
    last, last_ack, previous = None, 0.0, None
    def status(title, detail):
        nonlocal previous
        value = (title, detail)
        if value != previous:
            report(*value)
            previous = value
    status("Waiting for Factorio", "Start Factorio yourself and connect using the address above.")
    try:
        while not stop.is_set():
            try:
                beacon = read_json(path, 4096)
                if not isinstance(beacon, dict) or beacon.get("version") != 1:
                    raise ValueError("Invalid marker")
                if beacon.get("player") != profile["player"]:
                    status("Account name differs", "Choose the account and data folder used by this Factorio installation.")
                    stop.wait(0.5)
                    continue
                session, seq = beacon.get("session"), beacon.get("seq")
                if not isinstance(session, str) or not re.fullmatch(r"[0-9a-f]{32}", session) or type(seq) is not int:
                    raise ValueError("Invalid marker")
                key = (session, seq)
                if key != last:
                    try:
                        conn.request("POST", "/ack", {"player": profile["player"], "session": session, "seq": seq}, profile["token"])
                    except ServerResponse as exc:
                        if exc.code == 409:
                            last = key
                        raise
                    last, last_ack = key, time.monotonic()
                    status("Connected — reporting simulation steps", "Keep the companion open while playing. You can minimize it.")
                elif time.monotonic() - last_ack > 8:
                    status("Waiting for fresh simulation steps", "Normal while disconnected, paused or loading. In-game, check the data folder.")
            except FileNotFoundError:
                status("Waiting for Factorio", "Start Factorio and connect manually. If already playing, check the data folder.")
            except ServerResponse as exc:
                if exc.code == 403:
                    status("Access needs renewal", "Open Advanced and choose Repair access; no JSON download is needed.")
                    return
                if exc.code == 409:
                    status("Waiting for current server session", "Join this server and wait for a fresh simulation marker.")
                else:
                    status("Reporting connection interrupted", f"HTTP {exc.code}; retrying automatically.")
                stop.wait(0.5)
            except (TimeoutError, ConnectionError, http.client.HTTPException):
                status("Reporting connection interrupted", "The companion will reconnect automatically.")
                stop.wait(0.5)
            except (OSError, ValueError, TypeError):
                status("Waiting for a readable simulation marker", "Retrying automatically. In-game, check the selected data folder.")
                stop.wait(0.2)
            stop.wait(0.05)
    finally:
        conn.close()

def watch_server(server, stop, report, *, allow_local=False):
    conn = Connection(server, allow_local)
    try:
        while not stop.is_set():
            try:
                value = conn.request("GET", "/status")
                if not isinstance(value, dict) or value.get("version") != 1:
                    raise ValueError("Invalid status")
                report(value)
            except (OSError, ValueError, http.client.HTTPException):
                report(None)
            stop.wait(2)
    finally:
        conn.close()

def format_status(value):
    if not value or value.get("state") in {"stale", "starting", "reconnecting"}:
        return "Server status unavailable", "Waiting for a fresh controller update.", ""
    if value.get("state") == "idle":
        return "Paused · no players", "The factory resumes when a player joins.", ""
    def number(key):
        v = value.get(key)
        return f"{v:.1f}" if isinstance(v, (int, float)) and math.isfinite(v) else "—"
    headline = f"{number('measured_ups')} measured UPS · {number('target_ups')} target · {number('maximum_ups')} maximum"
    reason = str(value.get("reason", "Waiting for controller"))
    countdown = value.get("next_increase_seconds")
    if isinstance(countdown, (int, float)) and math.isfinite(countdown):
        reason += f"\nNext increase in about {math.ceil(max(0, countdown))}s if reports stay timely."
    labels = {"keeping_up": "keeping up", "reports_delayed": "delayed reports", "waiting_for_reports": "waiting for reports", "calibrating": "learning connection delay", "settling": "waiting for delay to settle"}
    peers = "\n".join(f"{p.get('player', '?')}: {labels.get(p.get('state'), 'unknown')}" for p in value.get("players", []) if isinstance(p, dict))
    return headline, reason, peers

class Application:
    def __init__(self, root, profile_path=None, *, autostart=True):
        self.root = root
        root.title(f"Adaptive UPS {VERSION}")
        root.geometry("760x770")
        root.minsize(700, 720)
        self.events = queue.Queue()
        self.stop, self.status_stop = threading.Event(), threading.Event()
        self.generation = 0
        self.profile, self.mod_directory, self.server = None, None, SETUP_URL
        self.settings_path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "FactorioAdaptiveUPS/settings.json"
        self.cache_path = self.settings_path.parent / "player-profile.json"
        self.directories = find_data_directories()
        settings, imported = {}, None
        try:
            settings = read_json(self.settings_path)
            if not isinstance(settings, dict):
                settings = {}
        except (OSError, ValueError):
            pass
        try:
            imported = load_profile(profile_path or settings.get("profile_path") or self.cache_path)
        except (OSError, ValueError, TypeError):
            pass
        directory = settings.get("data_directory") or (imported or {}).get("data_directory") or (self.directories[0] if len(self.directories) == 1 else "")
        if imported:
            self.profile, self.server = imported, imported["server"]
            self.mod_directory = imported.get("mod_directory")
        self.directory = tk.StringVar(value=directory)
        self.player = tk.StringVar(value=settings.get("player") or (imported or {}).get("player") or detect_player(directory))
        self.status = tk.StringVar(value="Preparing automatic setup…")
        self.detail = tk.StringVar(value="No access-file download or import is needed.")
        self.server_status = tk.StringVar(value="Reading server status…")
        self.server_reason, self.peers = tk.StringVar(), tk.StringVar()
        box = ttk.Frame(root, padding=22)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="Adaptive UPS", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(8, 4))
        ttk.Label(row, text=GAME_ADDRESS, font=("Consolas", 15)).pack(side="left")
        ttk.Button(row, text="Copy address", command=self.copy_address).pack(side="left", padx=12)
        ttk.Label(box, text="Keep this companion open. Start Factorio and connect to the address yourself.", wraplength=690).pack(anchor="w", pady=(2, 14))
        ttk.Label(box, text="Factorio data folder").pack(anchor="w")
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(3, 8))
        folder = ttk.Combobox(row, textvariable=self.directory, values=self.directories, state="readonly")
        folder.pack(side="left", fill="x", expand=True)
        folder.bind("<<ComboboxSelected>>", self.directory_changed)
        ttk.Button(row, text="Choose…", command=self.choose_directory).pack(side="left", padx=(6, 0))
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(0, 10))
        ttk.Label(row, text="Account name").pack(side="left")
        name = ttk.Entry(row, textvariable=self.player, width=25)
        name.pack(side="left", padx=8)
        name.bind("<Return>", lambda _: self.configure())
        ttk.Button(row, text="Apply changes", command=self.configure).pack(side="left")
        ttk.Separator(box).pack(fill="x", pady=(0, 12))
        ttk.Label(box, textvariable=self.status, font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(box, textvariable=self.detail, wraplength=690).pack(anchor="w", pady=(5, 12))
        ttk.Label(box, text="SERVER", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ttk.Label(box, textvariable=self.server_status, font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(5, 3))
        ttk.Label(box, textvariable=self.server_reason, wraplength=690).pack(anchor="w")
        peer_box = ttk.Frame(box)
        peer_box.pack(fill="x", pady=(6, 10))
        self.peer_list = tk.Text(peer_box, height=5, wrap="word", borderwidth=0, font=("Segoe UI", 9), state="disabled")
        self.peer_list.pack(side="left", fill="x", expand=True)
        peer_scroll = ttk.Scrollbar(peer_box, orient="vertical", command=self.peer_list.yview)
        peer_scroll.pack(side="right", fill="y")
        self.peer_list.configure(yscrollcommand=peer_scroll.set)
        ttk.Label(box, text="Late reports can mean queued simulation steps, a network delay, or a missing companion.", wraplength=690, foreground="#555555").pack(anchor="w")
        bottom = ttk.Frame(box)
        bottom.pack(side="bottom", fill="x", pady=(14, 0))
        ttk.Button(bottom, text="Why a mod and companion?", command=lambda: messagebox.showinfo("How Adaptive UPS works", EXPLANATION)).pack(side="left")
        ttk.Button(bottom, text="Advanced…", command=self.advanced).pack(side="right")
        ttk.Label(box, text="Until Mod Portal publication: install the bundled mod once, with Factorio closed.", wraplength=690).pack(anchor="w", pady=(14, 4))
        ttk.Button(box, text="Install bundled adaptive mod…", command=self.install).pack(anchor="w")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.drain)
        if autostart:
            self.start_status()
            root.after(150, self.configure)

    def start_status(self):
        self.status_stop.set()
        self.status_stop = threading.Event()
        stop = self.status_stop
        threading.Thread(target=watch_server, args=(self.server, stop, lambda v: self.events.put(("server", stop, v))), daemon=True).start()

    def copy_address(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(GAME_ADDRESS)

    def save_settings(self):
        save_json(self.settings_path, {"data_directory": self.directory.get(), "player": self.player.get().strip(), "profile_path": str(self.cache_path)})

    def configure(self, renew=False):
        self.stop.set()
        self.stop = threading.Event()
        self.generation += 1
        generation = self.generation
        player, directory = self.player.get().strip(), self.directory.get()
        if not directory or not Path(directory).is_dir():
            self.status.set("Choose your Factorio data folder")
            self.detail.set("Several installations may be present. Choose the folder containing the mods and script-output used by your game.")
            return
        if not player:
            player = detect_player(directory)
            self.player.set(player)
        if not PLAYER.fullmatch(player):
            self.status.set("Enter your Factorio account name")
            self.detail.set("Sign in to Factorio first, or enter the name here and choose Apply changes.")
            return
        self.status.set("Preparing reporting access…")
        self.detail.set("Your Factorio sign-in token stays on this computer.")
        existing = self.profile
        def prepare():
            try:
                profile = existing if existing and existing["player"] == player and not renew else get_or_create_profile(self.cache_path, player, self.server, renew=renew)
                self.events.put(("profile", generation, profile))
            except (OSError, ValueError, http.client.HTTPException) as exc:
                detail = "Check your connection, then choose Apply changes to retry."
                if isinstance(exc, ServerResponse) and exc.code == 429:
                    detail = "Setup requests are temporarily limited. Wait a minute, then choose Apply changes."
                self.events.put(("report", generation, ("Automatic setup could not finish", detail)))
        threading.Thread(target=prepare, daemon=True).start()

    def begin_reporting(self, profile):
        try:
            # Remember the selected folders in the cached profile as well.
            profile = {**profile, "data_directory": self.directory.get()}
            if self.mod_directory:
                profile["mod_directory"] = self.mod_directory
            else:
                profile.pop("mod_directory", None)
            save_json(self.cache_path, profile)
            self.save_settings()
        except OSError:
            self.status.set("Cannot save companion settings")
            self.detail.set("Check access to your LocalAppData folder, then choose Apply changes.")
            return
        self.profile = profile
        generation = self.generation
        threading.Thread(target=watch_progress, args=(dict(profile), self.directory.get(), self.stop,
            lambda *v: self.events.put(("report", generation, v))), daemon=True).start()

    def directory_changed(self, _event=None):
        self.mod_directory = None
        self.player.set(detect_player(self.directory.get()))
        self.configure()

    def choose_directory(self):
        path = filedialog.askdirectory(title="Factorio data folder — contains mods and script-output")
        if path:
            self.directory.set(path)
            self.directory_changed()

    def install(self):
        try:
            directory = self.directory.get()
            if not directory or not Path(directory).is_dir():
                raise ValueError("Choose an existing Factorio data folder first.")
            resources = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "resources"
            source = resources / MOD_FILE
            destination = Path(self.mod_directory or Path(directory) / "mods")
            target = destination / MOD_FILE
            already = target.is_file() and source.is_file() and target.read_bytes() == source.read_bytes()
            install_mod(source, destination)
            messagebox.showinfo("Mod already installed" if already else "Mod installed", "The matching mod is already installed. No files were changed." if already else "Installed the bundled adaptive mod. Start or restart Factorio yourself, then accept its server mod-sync prompt.")
        except (OSError, ValueError) as exc:
            messagebox.showerror("Mod installation failed", str(exc))

    def import_profile(self):
        path = filedialog.askopenfilename(title="Import an existing Adaptive UPS profile", filetypes=[("Helper profile", "*.json")])
        if path:
            try:
                profile = load_profile(path)
                self.profile, self.server = profile, profile["server"]
                self.player.set(profile["player"])
                if profile.get("data_directory"):
                    self.directory.set(profile["data_directory"])
                self.mod_directory = profile.get("mod_directory")
                self.start_status()
                self.configure()
            except (OSError, ValueError) as exc:
                messagebox.showerror("Could not import helper profile", str(exc))

    def advanced(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Advanced setup")
        box = ttk.Frame(dialog, padding=20)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="Ordinary setup handles reporting access automatically.").pack(anchor="w", pady=(0, 12))
        ttk.Button(box, text="Import an existing helper profile…", command=self.import_profile).pack(anchor="w", pady=5)
        ttk.Button(box, text="Repair access", command=lambda: (dialog.destroy(), self.configure(renew=True))).pack(anchor="w", pady=5)

    def drain(self):
        try:
            while True:
                kind, generation, value = self.events.get_nowait()
                if kind == "server":
                    if generation is self.status_stop:
                        headline, reason, peers = format_status(value)
                        self.server_status.set(headline)
                        self.server_reason.set(reason)
                        self.peers.set(peers)
                        self.peer_list.configure(state="normal")
                        self.peer_list.delete("1.0", "end")
                        self.peer_list.insert("1.0", peers)
                        self.peer_list.configure(state="disabled")
                elif generation == self.generation:
                    if kind == "profile":
                        self.begin_reporting(value)
                    else:
                        self.status.set(value[0])
                        self.detail.set(value[1])
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def close(self):
        self.stop.set()
        self.status_stop.set()
        self.root.destroy()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", help="Optional existing helper profile")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--start", action="store_true", help="Compatibility option; reporting now starts automatically")
    args = parser.parse_args()
    root = tk.Tk()
    if args.smoke_test:
        root.withdraw()
    app = Application(root, args.profile, autostart=not args.smoke_test)
    if args.smoke_test:
        root.after(300, app.close)
    root.mainloop()

if __name__ == "__main__":
    main()
