#!/usr/bin/env python3
"""
Flask / Socket-IO SSH tunnel manager (Refactored)
------------------------------------------------
This version simplifies state management, improves thread safety,
and uses standard logging for better reliability.
"""

import signal
import atexit
import os
import json
import subprocess
import secrets
import collections
import threading
import time
import logging
from functools import wraps
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO

# ------------------------------------------------------------------
# General Configuration & Logging
# ------------------------------------------------------------------
## Refactor: Centralized constants for easier configuration.
CONFIG_DIR = os.getenv("SSH_TUNNEL_CONFIG_DIR", "/data")
HOST_GATEWAY = os.getenv("DOCKER_HOST_GATEWAY", "host.docker.internal")
SSH_DIR     = os.path.join(CONFIG_DIR, ".ssh")
CONFIG_FILE = os.path.join(CONFIG_DIR, "ssh_tunnel_manager.json")
RETRY_BASE_SECONDS = 5
RETRY_MAX_SECONDS = 300

## Refactor: Use the standard logging module for thread-safe, formatted output.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

os.makedirs(SSH_DIR, exist_ok=True)
try:
    os.chmod(SSH_DIR, 0o700)
except Exception as e:
    logging.warning(f"Cannot chmod {SSH_DIR}: {e}")

# ------------------------------------------------------------------
# Flask / Socket-IO
# ------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = secrets.token_hex(16)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ------------------------------------------------------------------
# Global State with Thread Lock
# ------------------------------------------------------------------
## Refactor: Added a threading.Lock to protect shared access to the `connections` dict.
## This is CRITICAL for reliability to prevent race conditions.
connections_lock = threading.Lock()
connections = {}  # name -> SSHConnection

# ------------------------------------------------------------------
# SSHConnection Class (Simplified and More Self-Contained)
# ------------------------------------------------------------------
class SSHConnection:
    """
    Manages a single SSH tunnel subprocess, its state, and retry logic.
    """

    STATE_STOPPED = "Stopped"
    STATE_RUNNING = "Running"
    STATE_FAILED = "Failed"
    STATE_STARTING = "Starting"

    def __init__(
        self,
        name,
        local_port,
        remote_port,
        vps_ip,
        vps_user,
        key_path,
        ssh_port=22,
        exit_on_failure=True,
        autostart=False,
    ):
        self.name = name
        self.local_port = int(local_port)
        self.remote_port = int(remote_port)
        self.vps_ip = vps_ip
        self.vps_user = vps_user
        self.key_path = key_path
        self.ssh_port = int(ssh_port)
        self.exit_on_failure = bool(exit_on_failure)

        # Persisted user intent: auto-start unless manually stopped
        self.autostart = bool(autostart)

        # Runtime state
        self.process = None
        self.log_lines = collections.deque(maxlen=100)
        self.state = self.STATE_STOPPED
        self.status_message = "Not started yet."

        # Retry logic state
        self.retry_attempts = 0
        self.next_retry_time = 0

    def _build_cmd(self):
        dest = f"{self.vps_user}@{self.vps_ip}" if self.vps_user else self.vps_ip
        return [
            "ssh",
            "-vv",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            f"ExitOnForwardFailure={'yes' if self.exit_on_failure else 'no'}",
            "-N",
            "-R",
            f"0.0.0.0:{self.remote_port}:{HOST_GATEWAY}:{self.local_port}",
            "-i",
            self.key_path,
            "-p",
            str(self.ssh_port),
            dest,
        ]

    def _pump_stderr(self):
        """Background thread function to read stderr from the ssh process."""
        for line in self.process.stderr:
            line = line.decode(errors="replace").rstrip()
            if line:
                self.log_lines.append(line)
                logging.info(f"[{self.name}] {line}")
        logging.info(
            f"[{self.name}] Stderr stream finished, process has exited."
        )

    def start(self):
        """Initiates the connection attempt."""
        if self.process and self.process.poll() is None:
            logging.warning(
                f"[{self.name}] Start called but process is already running."
            )
            return True

        # When user starts, we set autostart True
        self.autostart = True
        self.next_retry_time = 0
        self.retry_attempts = 0

        self.state = self.STATE_STARTING
        self.status_message = "Attempting to start..."
        self.log_lines.clear()
        self.log_lines.append(
            f"Starting connection at "
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        try:
            os.chmod(self.key_path, 0o600)
        except Exception as e:
            self.status_message = "Key permission error"
            self.log_lines.append(f"Error: {e}")
            self.state = self.STATE_FAILED
            return False

        try:
            cmd = self._build_cmd()
            logging.info(f"[{self.name}] Spawning command: {' '.join(cmd)}")
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=1
            )
            threading.Thread(target=self._pump_stderr, daemon=True).start()
            self.retry_attempts = 0
            return True
        except Exception as e:
            self.status_message = "Failed to spawn ssh"
            self.log_lines.append(f"Error: {e}")
            self.state = self.STATE_FAILED
            return False

    def stop(self):
        """Stops the connection."""
        # When user stops, we set autostart False
        self.autostart = False
        self.state = self.STATE_STOPPED
        self.status_message = "Stopped by user."
        self.retry_attempts = 0
        self.next_retry_time = 0
        if self.process and self.process.poll() is None:
            logging.info(f"[{self.name}] Terminating process.")
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logging.warning(
                    f"[{self.name}] Process did not terminate, killing."
                )
                self.process.kill()
        self.process = None

    def update_status(self):
        """
        Checks the process and logs, and updates the connection's state.
        The single source of truth.
        """
        if self.state == self.STATE_STOPPED:
            return  # Don't check if it was manually stopped.

        if not self.process or self.process.poll() is not None:
            # Process is not running.
            if self.state != self.STATE_FAILED:
                self.status_message = "Connection failed."
                logging.warning(
                    f"[{self.name}] Connection died unexpectedly."
                )
            self.state = self.STATE_FAILED
            return

        # Process is running, check for common error messages in logs.
        error_patterns = [
            "connection refused",
            "permission denied",
            "port forwarding failed",
            "connection closed by",
            "connection reset by peer",
            "broken pipe",
            "timeout",
            "server not responding",
            "host key verification failed",
        ]
        recent_logs = list(self.log_lines)[-10:]
        for pattern in error_patterns:
            if any(pattern in line.lower() for line in recent_logs):
                self.state = self.STATE_FAILED
                self.status_message = (
                    f"Error detected in logs: '{pattern}'"
                )
                logging.warning(f"[{self.name}] {self.status_message}")
                return

        # If we reach here, the connection is considered healthy.
        self.state = self.STATE_RUNNING
        self.status_message = "Active"
        self.retry_attempts = 0

    def schedule_retry(self):
        """Sets the next retry time using exponential backoff."""
        self.retry_attempts += 1
        backoff = min(
            RETRY_BASE_SECONDS * (2 ** (self.retry_attempts - 1)),
            RETRY_MAX_SECONDS,
        )
        self.next_retry_time = time.time() + backoff
        self.status_message = (
            f"Connection failed. Retrying in {int(backoff)}s..."
        )
        logging.info(
            f"[{self.name}] Scheduling retry attempt "
            f"{self.retry_attempts} in {backoff:.0f} seconds."
        )

    def to_dict(self):
        """Serializes the connection object to a dictionary for the API/UI."""
        return {
            "name": self.name,
            "local_port": self.local_port,
            "remote_port": self.remote_port,
            "vps_ip": self.vps_ip,
            "vps_user": self.vps_user,
            "key_path": self.key_path,
            "ssh_port": self.ssh_port,
            "exit_on_failure": self.exit_on_failure,
            "autostart": self.autostart,
            "state": self.state,
            "status_message": self.status_message,
            "log": list(self.log_lines),
            # "active" kept for frontend compatibility
            "active": self.state == self.STATE_RUNNING,
        }

    @classmethod
    def from_dict(cls, data):
        """Creates a connection object from a dictionary."""
        # Back-compat for older configs: infer autostart if missing.
        prev_state = data.get("state")
        prev_active = data.get("active")
        autostart = data.get("autostart")
        if autostart is None:
            autostart = bool(prev_active) or (
                prev_state is not None and prev_state != cls.STATE_STOPPED
            )

        conn = cls(
            data["name"],
            data["local_port"],
            data["remote_port"],
            data["vps_ip"],
            data.get("vps_user"),
            data["key_path"],
            data.get("ssh_port", 22),
            data.get("exit_on_failure", True),
            autostart=autostart,
        )

        # On load, derive initial runtime state from intent:
        if conn.autostart:
            conn.state = cls.STATE_FAILED
            conn.status_message = "Scheduled for auto-restart."
        else:
            conn.state = cls.STATE_STOPPED
            conn.status_message = "Stopped (manual)."

        return conn

# ------------------------------------------------------------------
# Persistence (Thread-Safe)
# ------------------------------------------------------------------
def save_connections():
    with connections_lock:
        try:
            with open(CONFIG_FILE, "w") as fp:
                json.dump({name: conn.to_dict() for name, conn in connections.items()}, fp, indent=2)
        except Exception as e:
            logging.error(f"Cannot save {CONFIG_FILE}: {e}")

def load_connections():
    global connections
    if not os.path.exists(CONFIG_FILE):
        return
    try:
        with open(CONFIG_FILE, "r") as fp:
            data = json.load(fp)
        with connections_lock:
            connections = {name: SSHConnection.from_dict(cfg) for name, cfg in data.items()}
            logging.info(f"Loaded {len(connections)} connections from config.")
    except Exception as e:
        logging.error(f"Failed to load connections from {CONFIG_FILE}: {e}")

# ------------------------------------------------------------------
# Background Monitor (Simplified)
# ------------------------------------------------------------------
def connection_monitor():
    """Periodically checks connections and manages restarts."""
    logging.info("Connection monitor started.")
    while True:
        with connections_lock:
            conns_to_check = list(connections.values())

        changed = False
        now = time.time()

        for conn in conns_to_check:
            initial_state = conn.state
            conn.update_status()

            # Only manage connections that are intended to auto-start
            if not conn.autostart:
                if conn.state != initial_state:
                    changed = True
                continue

            if (
                conn.state == SSHConnection.STATE_FAILED
                and conn.retry_attempts == 0
            ):
                # Fresh failure or just loaded; schedule first retry
                conn.schedule_retry()
                changed = True
            elif (
                conn.state == SSHConnection.STATE_FAILED
                and now >= conn.next_retry_time
            ):
                # Time to retry a failed connection
                logging.info(f"[{conn.name}] Attempting scheduled restart.")
                if conn.start():
                    # Let next loop determine health
                    pass
                else:
                    conn.schedule_retry()
                changed = True

            if conn.state != initial_state:
                changed = True

        if changed:
            save_connections()
            with connections_lock:
                status_list = [c.to_dict() for c in connections.values()]
            socketio.emit("status_update", status_list)

        socketio.sleep(5)

# ------------------------------------------------------------------
# API Route Helpers / Decorators
# ------------------------------------------------------------------
## Refactor: A decorator to simplify API routes by handling common logic.
def get_connection_or_404(f):
    @wraps(f)
    def decorated_function(name, *args, **kwargs):
        with connections_lock:
            conn = connections.get(name)
        if not conn:
            return jsonify(error="Connection not found"), 404
        return f(conn, *args, **kwargs)
    return decorated_function

# ------------------------------------------------------------------
# Flask API Routes (Simplified with Decorator)
# ------------------------------------------------------------------
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/connections", methods=["GET"])
def api_list_connections():
    with connections_lock:
        return jsonify([c.to_dict() for c in connections.values()])

@app.route("/api/connections", methods=["POST"])
def api_add_connection():
    data = request.json
    # Basic validation
    required = ["name", "local_port", "remote_port", "vps_ip", "key_path"]
    if not all(k in data for k in required):
        return jsonify(error=f"Missing one of required fields: {required}"), 400

    with connections_lock:
        if data["name"] in connections:
            return jsonify(error="Name already exists"), 400
    if not os.path.exists(data["key_path"]):
        return jsonify(error=f"SSH key '{data['key_path']}' not found"), 400

    conn = SSHConnection.from_dict(data)
    with connections_lock:
        connections[conn.name] = conn
    save_connections()
    return jsonify(conn.to_dict()), 201

@app.route("/api/connections/<name>/start", methods=["POST"])
@get_connection_or_404
def api_start(conn):
    if conn.start():
        save_connections()
        return jsonify(conn.to_dict())
    return jsonify(error="Failed to start process", details=conn.status_message), 500

@app.route("/api/connections/<name>/stop", methods=["POST"])
@get_connection_or_404
def api_stop(conn):
    conn.stop()
    save_connections()
    return jsonify(conn.to_dict())

@app.route("/api/connections/<name>", methods=["DELETE"])
@get_connection_or_404
def api_delete(conn):
    conn.stop()
    with connections_lock:
        del connections[conn.name]
    save_connections()
    return jsonify(success=True)

# NOTE: The SSH key management helpers are already quite clean and don't need major refactoring.
# I'm including them as-is for completeness.
@app.route("/api/ssh-keys", methods=["GET"])
def api_list_keys():
    keys = []
    if not os.path.isdir(SSH_DIR):
        return jsonify(keys)
    for fname in os.listdir(SSH_DIR):
        if fname.endswith(".pub"):
            continue
        p = os.path.join(SSH_DIR, fname)
        if os.path.isfile(p):
            keys.append({
                "name": fname, "path": p,
                "has_public_key": os.path.exists(p + ".pub")
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
        # Using a more modern library for key generation
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives import serialization as crypto_serialization
        key = ed25519.Ed25519PrivateKey.generate()

        with open(path, "wb") as f:
            os.chmod(path, 0o600)
            f.write(key.private_bytes(
                crypto_serialization.Encoding.PEM,
                crypto_serialization.PrivateFormat.OpenSSH,
                crypto_serialization.NoEncryption()
            ))

        with open(path + ".pub", "wb") as f:
            f.write(key.public_key().public_bytes(
                crypto_serialization.Encoding.OpenSSH,
                crypto_serialization.PublicFormat.OpenSSH
            ))

    except Exception as e:
        logging.error(f"Failed to generate key: {e}")
        return jsonify(error=str(e)), 500
    return jsonify(success=True)

@app.route("/api/ssh-keys/<name>/public", methods=["GET"])
def api_public_key(name):
    path = os.path.join(SSH_DIR, name + ".pub")
    try:
        with open(path, "r") as f:
            return jsonify(public_key=f.read().strip())
    except FileNotFoundError:
        return jsonify(error="Public key not found"), 404

# ------------------------------------------------------------------
# App Start-up
# ------------------------------------------------------------------

def _persist_on_shutdown(signum=None, frame=None):
    try:
        logging.info("Shutting down, saving connections...")
        save_connections()
    except Exception as e:
        logging.error(f"Error saving on shutdown: {e}")

if __name__ == "__main__":
    load_connections()

    # Save state on SIGTERM/SIGINT and normal exit
    signal.signal(signal.SIGTERM, _persist_on_shutdown)
    signal.signal(signal.SIGINT, _persist_on_shutdown)
    atexit.register(_persist_on_shutdown)

    socketio.start_background_task(connection_monitor)
    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=False,
        allow_unsafe_werkzeug=True,  # Required for this setup in recent versions.
    )