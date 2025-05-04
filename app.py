#!/usr/bin/env python3
"""
Flask / Socket‑IO SSH tunnel manager
-----------------------------------
Copy this file next to *templates/index.html* and run

    python3 app.py

Open   http://localhost:5000   in your browser.
"""

import os
import json
import subprocess
import secrets
import paramiko
import collections
import threading
import time
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO

# ------------------------------------------------------------------
# General configuration
# ------------------------------------------------------------------
HOST_GATEWAY = "host.docker.internal"
CONFIG_DIR = "/data"      # change if you wish
SSH_DIR     = os.path.join(CONFIG_DIR, ".ssh")
CONFIG_FILE = os.path.join(CONFIG_DIR, "ssh_tunnel_manager.json")

os.makedirs(SSH_DIR, exist_ok=True)
try:
    os.chmod(SSH_DIR, 0o700)
except Exception as exc:
    print(f"Warning – cannot chmod {SSH_DIR}: {exc}")

# ------------------------------------------------------------------
# Flask / Socket‑IO
# ------------------------------------------------------------------
app       = Flask(__name__)
app.config["SECRET_KEY"] = secrets.token_hex(16)

# We force *threading* mode – we are using normal python threads
socketio  = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ------------------------------------------------------------------
# SSHConnection class
# ------------------------------------------------------------------
class SSHConnection:
    """
    Wrapper around an ssh -R command that keeps a small live log of
    everything the ssh client writes to stderr.
    """
    def __init__(self, name, local_port, remote_port,
                 vps_ip, vps_user, key_path,
                 ssh_port=22, alive_interval=60, exit_on_failure=True):
        # basic fields -------------------------------------------------
        self.name            = name
        self.local_port      = local_port
        self.remote_port     = remote_port
        self.vps_ip          = vps_ip
        self.vps_user        = vps_user
        self.key_path        = key_path
        self.ssh_port        = ssh_port
        self.alive_interval  = alive_interval
        self.exit_on_failure = exit_on_failure

        # runtime state -----------------------------------------------
        self.process         = None          #   subprocess.Popen
        self.stderr_thread   = None          #   Thread reading stderr
        self.log_lines       = collections.deque(maxlen=100)   # last 100
        self.active          = False
        self.status_message  = ""
        self.last_error      = ""

    # -----------------------------------------------------------------
    # helpers
    # -----------------------------------------------------------------
    def _build_cmd(self):
        dest = f"{self.vps_user+'@' if self.vps_user else ''}{self.vps_ip}"
        return [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-N",
            "-R", f"0.0.0.0:{self.remote_port}:{HOST_GATEWAY}:{self.local_port}",
            "-i", self.key_path,
            "-p", str(self.ssh_port),
            "-o", f"ServerAliveInterval={self.alive_interval}",
            "-o", f"ExitOnForwardFailure={'yes' if self.exit_on_failure else 'no'}",
            "-v",                 # <-- 1× verbose; add -vv if you want more
            dest,
        ]

    def _pump_stderr(self):
        """
        Runs in background; streams ssh's stderr to console and buffer.
        """
        for line in self.process.stderr:
            line = line.decode(errors="replace").rstrip()
            if line:
                self.log_lines.append(line)
                print(f"[{self.name}] {line}")       # <-- visible in docker logs
        # when loop exits the process is dead; is_active() will notice

    # -----------------------------------------------------------------
    # public control methods
    # -----------------------------------------------------------------
    def start(self):
        if self.process and self.process.poll() is None:
            self.status_message = "Already running"
            return True

        try:
            os.chmod(self.key_path, 0o600)
        except Exception as exc:
            self.last_error = f"chmod key failed: {exc}"
            self.status_message = "Key permission error"
            return False

        # Clear previous log entries when starting fresh
        self.log_lines.clear()
        self.log_lines.append(f"Starting connection at {time.strftime('%Y-%m-%d %H:%M:%S')}")

        try:
            self.process = subprocess.Popen(
                self._build_cmd(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=1
            )
        except Exception as exc:
            self.last_error = str(exc)
            self.status_message = "Failed to spawn ssh"
            self.log_lines.append(f"Error: {self.last_error}")
            return False

        self.stderr_thread = threading.Thread(
            target=self._pump_stderr, daemon=True)
        self.stderr_thread.start()

        # Give ssh a couple of seconds to establish connection
        for _ in range(6):
            time.sleep(0.5)
            if self.process.poll() is not None:
                break

        if self.process.poll() is not None:  # died already
            self.active = False
            self.status_message = "Failed to start"
            self.last_error = "\n".join(list(self.log_lines)[-10:])  # Last 10 lines
            return False

        self.active = True
        self.status_message = "Active"
        self.last_error = ""
        return True

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.active = False
        self.status_message = "Stopped"

    def is_active(self):
        if self.process and self.process.poll() is None:
           if any("Connection refused" in line for line in self.log_lines):
               self.status_message = "No listener on local port"
           else:
               self.status_message = "Active"
           return True
        if self.active:                               # died meanwhile
            self.active = False
            self.status_message = "Connection failed"
            self.last_error = "\n".join(self.log_lines)
        return False

    def check_health(self):
        """
        Performs a deeper health check beyond just checking if the process is running.
        Returns a tuple (is_healthy, message)
        """
        if not self.process or self.process.poll() is not None:
            return False, "Process not running"

        # Check for common error patterns in logs
        recent_logs = list(self.log_lines)[-20:]  # Last 20 log entries

        if any("Connection refused" in line for line in recent_logs):
            return False, "No listener on local port"
        if any("Connection closed" in line for line in recent_logs):
            return False, "Connection closed by remote host"
        if any("Permission denied" in line for line in recent_logs):
            return False, "SSH authentication failed"
        if any("Host key verification failed" in line for line in recent_logs):
            return False, "Host key verification failed"

        return True, "Healthy"

    # -----------------------------------------------------------------
    # (de)serialisation helpers
    # -----------------------------------------------------------------
    def to_dict(self):
        return {
            "name"          : self.name,
            "local_port"    : self.local_port,
            "remote_port"   : self.remote_port,
            "vps_ip"        : self.vps_ip,
            "vps_user"      : self.vps_user,
            "key_path"      : self.key_path,
            "ssh_port"      : self.ssh_port,
            "alive_interval": self.alive_interval,
            "exit_on_failure":self.exit_on_failure,
            "active"        : self.is_active(),
            "status_message": self.status_message,
            "last_error"    : self.last_error,
            "log"           : list(self.log_lines)    # <-- new!
        }

    @classmethod
    def from_dict(cls, d):
        obj = cls(
            d["name"], d["local_port"], d["remote_port"],
            d["vps_ip"], d["vps_user"], d["key_path"],
            d.get("ssh_port", 22), d.get("alive_interval", 60),
            d.get("exit_on_failure", True),
        )
        obj.status_message = d.get("status_message", "")
        obj.last_error     = d.get("last_error", "")
        obj.active         = d.get("active", False)     # <- keep flag
        return obj

# ------------------------------------------------------------------
# Persistence
# ------------------------------------------------------------------
connections = {}        # name -> SSHConnection

def load_connections():
    global connections
    if not os.path.exists(CONFIG_FILE):
        return

    try:
        with open(CONFIG_FILE, "r") as fp:
            data = json.load(fp)

        # recreate objects
        connections = {name: SSHConnection.from_dict(cfg)
                       for name, cfg in data.items()}

        # auto‑start ones that were previously running
        for conn in connections.values():
            if conn.active:                 # flag was true in JSON
                print(f"[startup] auto‑starting {conn.name}")
                conn.start()                # best effort – sets .active etc.
    except Exception as exc:
        print(f"Failed to load connections: {exc}")

def save_connections():
    try:
        with open(CONFIG_FILE, "w") as fp:
            json.dump({n:c.to_dict() for n,c in connections.items()},
                      fp, indent=2)
    except Exception as exc:
        print(f"Cannot save {CONFIG_FILE}: {exc}")

# ------------------------------------------------------------------
# Background monitor
# ------------------------------------------------------------------
def connection_monitor():
    """
    Runs forever. Monitors tunnels and implements a robust reconnection strategy
    with exponential backoff for failed connections.
    """
    # Track retry attempts and next retry time for each connection
    retry_state = {}  # name -> {"attempts": int, "next_retry": timestamp}

    while True:
        current_time = time.time()
        changed = False

        for conn in list(connections.values()):
            # Use check_health for more comprehensive status checking
            is_healthy, health_msg = conn.check_health()
            was = conn.active
            now = is_healthy
            conn.status_message = health_msg  # Update status message

            # Initialize retry state if not exists
            if conn.name not in retry_state:
                retry_state[conn.name] = {"attempts": 0, "next_retry": 0}

            # Connection died
            if was and not now:
                print(f"{conn.name} died – attempting to restart")
                conn.start()
                changed = True
                # Reset retry counter on fresh failure
                retry_state[conn.name]["attempts"] = 1
                retry_state[conn.name]["next_retry"] = current_time + 5

            # Connection is inactive but should be retried
            elif not now and retry_state[conn.name]["attempts"] > 0:
                # Check if it's time for a retry
                if current_time >= retry_state[conn.name]["next_retry"]:
                    attempts = retry_state[conn.name]["attempts"]
                    # Exponential backoff: 5, 10, 20, 40, 80... seconds up to 5 minutes
                    backoff = min(5 * (2 ** (attempts - 1)), 300)

                    print(f"{conn.name} reconnection attempt {attempts} after {backoff}s backoff")
                    if conn.start():
                        # Success - reset retry state
                        retry_state[conn.name]["attempts"] = 0
                    else:
                        # Failed - increment counter and set next retry time
                        retry_state[conn.name]["attempts"] += 1
                        retry_state[conn.name]["next_retry"] = current_time + backoff

                    changed = True

            # Connection status changed (other than the cases above)
            elif was != now:
                changed = True

            # Clean up retry state for connections that are now active
            if now and retry_state[conn.name]["attempts"] > 0:
                retry_state[conn.name]["attempts"] = 0

        # Remove retry state for connections that no longer exist
        for name in list(retry_state.keys()):
            if name not in connections:
                del retry_state[name]

        if changed:
            save_connections()
            socketio.emit("status_update", [c.to_dict() for c in connections.values()])

        socketio.sleep(5)

socketio.start_background_task(connection_monitor)

# ------------------------------------------------------------------
# Flask routes / API
# ------------------------------------------------------------------
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/connections", methods=["GET"])
def api_list_connections():
    return jsonify([c.to_dict() for c in connections.values()])

@app.route("/api/connections", methods=["POST"])
def api_add_connection():
    data = request.json or {}
    for field in ("name","local_port","remote_port","vps_ip","key_path"):
        if not data.get(field):
            return jsonify(error=f"Missing {field}"), 400
    if data["name"] in connections:
        return jsonify(error="Name already exists"), 400
    if not os.path.exists(data["key_path"]):
        return jsonify(error="SSH key not found"), 400

    conn = SSHConnection(
        name           = data["name"],
        local_port     = int(data["local_port"]),
        remote_port    = int(data["remote_port"]),
        vps_ip         = data["vps_ip"],
        vps_user       = data.get("vps_user",""),
        key_path       = data["key_path"],
        ssh_port       = int(data.get("ssh_port",22)),
        alive_interval = int(data.get("alive_interval",60)),
        exit_on_failure= bool(data.get("exit_on_failure",True)),
    )
    connections[conn.name] = conn
    save_connections()
    return jsonify(success=True, connection=conn.to_dict())

@app.route("/api/connections/<name>/start", methods=["POST"])
def api_start(name):
    conn = connections.get(name)
    if not conn:
        return jsonify(error="Not found"), 404
    if conn.start():
        save_connections()
        return jsonify(success=True, connection=conn.to_dict())
    return jsonify(error=conn.last_error or "Failed"), 500

@app.route("/api/connections/<name>/stop", methods=["POST"])
def api_stop(name):
    conn = connections.get(name)
    if not conn:
        return jsonify(error="Not found"), 404
    conn.stop()
    save_connections()
    return jsonify(success=True, connection=conn.to_dict())

@app.route("/api/connections/<name>", methods=["PUT"])
def api_update(name):
    conn = connections.get(name)
    if not conn:
        return jsonify(error="Not found"), 404
    data = request.json or {}
    # rename
    new_name = data.get("name", name)
    if new_name != name and new_name in connections:
        return jsonify(error="Name already exists"), 400
    if new_name != name:
        del connections[name]
        conn.name = new_name
        connections[new_name] = conn
    # update fields
    for field in ("local_port","remote_port","vps_ip","vps_user",
                  "key_path","ssh_port","alive_interval","exit_on_failure"):
        if field in data:
            setattr(conn, field, data[field])
    save_connections()
    return jsonify(success=True, connection=conn.to_dict())

@app.route("/api/connections/<name>", methods=["DELETE"])
def api_delete(name):
    conn = connections.pop(name, None)
    if not conn:
        return jsonify(error="Not found"), 404
    conn.stop()
    save_connections()
    return jsonify(success=True)

# ------------------------------------------------------------------
# SSH key helpers
# ------------------------------------------------------------------
@app.route("/api/ssh-keys", methods=["GET"])
def api_list_keys():
    keys = []
    for fname in os.listdir(SSH_DIR):
        if fname.endswith(".pub"):
            continue
        p = os.path.join(SSH_DIR, fname)
        if os.path.isfile(p):
            keys.append({
                "name": fname,
                "path": p,
                "has_public_key": os.path.exists(p+".pub")
            })
    return jsonify(keys)

@app.route("/api/ssh-keys", methods=["POST"])
def api_generate_key():
    name = (request.json or {}).get("name")
    if not name:
        return jsonify(error="Key name required"), 400
    path = os.path.join(SSH_DIR, name)
    if os.path.exists(path):
        return jsonify(error="Key already exists"), 400
    try:
        key = paramiko.RSAKey.generate(2048)
        key.write_private_key_file(path)
        os.chmod(path, 0o600)
        with open(path+".pub","w") as fp:
            fp.write(f"ssh-rsa {key.get_base64()} ssh-tunnel-manager")
    except Exception as exc:
        return jsonify(error=str(exc)), 500
    return jsonify(success=True)

@app.route("/api/ssh-keys/<name>/public", methods=["GET"])
def api_public_key(name):
    path = os.path.join(SSH_DIR, name+".pub" if not name.endswith(".pub") else name)
    if not os.path.exists(path):
        return jsonify(error="Not found"), 404
    return jsonify(success=True, public_key=open(path).read().strip())

# ------------------------------------------------------------------
# App start‑up
# ------------------------------------------------------------------
load_connections()

if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=False,
        allow_unsafe_werkzeug=True       # <‑‑ add this line
    )

