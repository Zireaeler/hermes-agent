"""Phase 4G8 long-horizon qualification and fault-trigger infrastructure."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import tomllib
from typing import Any, Optional
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.parse
import urllib.request

from agent.redact import redact_sensitive_text


QUALIFICATION_SPEC_SCHEMA = "hermes_phase4g8_qualification_v1"
LOCKED_MANIFEST_SCHEMA = "hermes_phase4g8_locked_task_v1"
QUALIFICATION_REPORT_SCHEMA = "hermes_phase4g8_qualification_report_v1"
EVALUATOR_RESULT_SCHEMA = "hermes_phase4g8_evaluator_result_v1"
RUN_REPORT_SCHEMA = "hermes_phase4g8_run_report_v1"
AGGREGATE_REPORT_SCHEMA = "hermes_phase4g8_aggregate_report_v1"
FAULT_TRIGGERS = {
    "worker_running",
    "receipt_before_ingest",
    "lease_expired",
}
PROCESS_OWNER_ENV = "HERMES_PHASE4G8_RUN_ID"
PHASE4G8_CODEX_APPROVAL_POLICY = "on-request"
PHASE4G8_CODEX_APPROVAL_REVIEWER = "auto_review"
PHASE4G8_CODEX_AUTO_REVIEW_POLICY_VERSION = "phase4g8-dangerous-operations-v1"
PHASE4G8_CODEX_EXEC_POLICY_VERSION = "phase4g8-exec-policy-v1"
PHASE4G8_CODEX_AUTO_REVIEW_POLICY = """\
You review dangerous operations requested by an isolated Hermes Phase 4G8 benchmark worker.

Approve routine repository inspection, workspace-local edits, builds, and tests when their intended
effects remain inside the current workspace or isolated temporary storage and they use only the
preinstalled read-only toolchain and normal system runtime.

Deny requests that attempt to read credentials, auth files, API keys, tokens, user-home secrets, or
unrelated host configuration; access protected benchmark oracle material, gold patches, hidden test
patches, evaluator sources, or qualification artifacts; escape the current workspace to modify or
delete host files; use sudo, su, setuid, capabilities, mounts, namespace controls, firewall controls,
or other privilege-escalation mechanisms; signal unrelated host processes; contact external networks
from worker tools; or weaken/disable the outer sandbox, network namespace, audit trail, or reviewer.

Allow bounded cleanup only for generated files inside the current workspace or isolated temporary
storage. Deny recursive deletion of the repository root or source material. When scope, target, or
side effects are uncertain, deny the request.
"""
PHASE4G8_CODEX_EXEC_POLICY = """\
prefix_rule(
    pattern = [["sudo", "su", "doas", "pkexec"]],
    decision = "forbidden",
    justification = "Phase 4G8 workers cannot elevate privileges.",
)
prefix_rule(
    pattern = [["mount", "umount", "nsenter", "unshare", "ip", "nft", "iptables", "capsh", "setpriv"]],
    decision = "forbidden",
    justification = "Phase 4G8 workers cannot alter host isolation or capabilities.",
)
prefix_rule(
    pattern = [["systemctl", "service", "docker", "podman", "reboot", "shutdown"]],
    decision = "forbidden",
    justification = "Phase 4G8 workers cannot control host services or runtimes.",
)
prefix_rule(
    pattern = [["curl", "wget", "ssh", "scp", "sftp", "nc", "ncat", "socat"]],
    decision = "forbidden",
    justification = "Worker tool network is disabled; model transport uses the isolated proxy.",
)
prefix_rule(
    pattern = ["git", ["push", "fetch", "pull", "clone"]],
    decision = "forbidden",
    justification = "Phase 4G8 benchmark workers cannot use Git network operations.",
)
prefix_rule(
    pattern = [["apt", "apt-get", "apk", "dnf", "yum", "brew"]],
    decision = "forbidden",
    justification = "The prepared Phase 4G8 toolchain is immutable.",
)
prefix_rule(
    pattern = [["rm", "chmod", "chown", "kill", "pkill", "killall", "truncate", "shred"]],
    decision = "prompt",
    justification = "Review destructive or permission-changing operations against the isolated workspace boundary.",
)
prefix_rule(
    pattern = ["git", ["reset", "clean", "restore", "checkout"]],
    decision = "prompt",
    justification = "Review Git operations that can discard workspace changes.",
)
prefix_rule(
    pattern = [["pip", "pip3"], "install"],
    decision = "prompt",
    justification = "Review attempts to mutate the prepared Python environment.",
)
prefix_rule(
    pattern = [["npm", "pnpm", "yarn"], ["install", "add", "publish"]],
    decision = "prompt",
    justification = "Review package installation or publication requests.",
)
"""


class _ModelProxyHandler(BaseHTTPRequestHandler):
    server: "_ModelProxyServer"

    def do_GET(self) -> None:  # noqa: N802
        self._forward()

    def do_POST(self) -> None:  # noqa: N802
        self._forward()

    def _forward(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if not any(parsed.path == prefix or parsed.path.startswith(prefix + "/") for prefix in self.server.allowed_paths):
            self.send_error(403, "model proxy path denied")
            return
        if str(self.headers.get("Upgrade") or "").lower() == "websocket":
            self.server.record_transport_event("websocket_upgrade_attempt_count")
            self._forward_websocket(parsed)
            return
        self.server.record_transport_event("http_request_count")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        target = urllib.parse.urlunsplit(
            (
                self.server.upstream.scheme,
                self.server.upstream.netloc,
                parsed.path,
                parsed.query,
                "",
            )
        )
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "connection", "content-length", "accept-encoding"}
        }
        request = urllib.request.Request(target, data=body, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(request, timeout=self.server.upstream_timeout_seconds) as response:
                self.send_response(response.status)
                for key, value in response.headers.items():
                    if key.lower() not in {"connection", "transfer-encoding", "content-length", "content-encoding"}:
                        self.send_header(key, value)
                self.end_headers()
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except urllib.error.HTTPError as exc:
            try:
                self.send_response(exc.code)
                self.end_headers()
                self.wfile.write(exc.read())
            except (BrokenPipeError, ConnectionResetError):
                return
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            try:
                self.send_error(502, "model transport unavailable")
            except (BrokenPipeError, ConnectionResetError):
                return

    def _forward_websocket(self, parsed: urllib.parse.SplitResult) -> None:
        upstream_socket: Optional[socket.socket] = None
        response_started = False
        upgrade_succeeded = False
        self.close_connection = True
        try:
            host = self.server.upstream.hostname
            if not host:
                raise RuntimeError("model websocket upstream host is missing")
            secure = self.server.upstream.scheme == "https"
            port = self.server.upstream.port or (443 if secure else 80)
            raw_socket = socket.create_connection(
                (host, port),
                timeout=self.server.upstream_timeout_seconds,
            )
            if secure:
                context = ssl.create_default_context()
                upstream_socket = context.wrap_socket(raw_socket, server_hostname=host)
            else:
                upstream_socket = raw_socket
            target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            headers = [
                f"{self.command} {target} HTTP/1.1",
                f"Host: {self.server.upstream.netloc}",
            ]
            for key, value in self.headers.items():
                if key.lower() in {"host", "proxy-connection", "content-length"}:
                    continue
                headers.append(f"{key}: {value}")
            upstream_socket.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("latin-1"))
            handshake = _read_http_header(
                upstream_socket,
                max_bytes=64 * 1024,
            )
            self.connection.sendall(handshake)
            response_started = True
            status_line = handshake.split(b"\r\n", 1)[0]
            if not status_line.startswith(b"HTTP/") or b" 101 " not in status_line:
                return
            self.server.record_transport_event("websocket_101_count")
            upgrade_succeeded = True
            _relay_bidirectional(
                self.connection,
                upstream_socket,
                idle_timeout_seconds=self.server.upstream_timeout_seconds,
            )
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception:
            if not response_started:
                try:
                    self.send_error(502, "model websocket transport unavailable")
                except (BrokenPipeError, ConnectionResetError):
                    pass
        finally:
            if not upgrade_succeeded:
                self.server.record_transport_event("websocket_failure_count")
            if upstream_socket is not None:
                try:
                    upstream_socket.close()
                except OSError:
                    pass

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class _ModelProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], upstream_base_url: str, timeout_seconds: float):
        self.upstream = urllib.parse.urlsplit(upstream_base_url)
        if self.upstream.scheme not in {"http", "https"} or not self.upstream.netloc:
            raise ValueError("model base URL must be absolute HTTP(S)")
        base_path = self.upstream.path.rstrip("/")
        self.allowed_paths = {
            base_path + "/responses",
            base_path + "/models",
        }
        self.upstream_timeout_seconds = float(timeout_seconds)
        self._transport_lock = threading.Lock()
        self._transport_counts = {
            "http_request_count": 0,
            "websocket_upgrade_attempt_count": 0,
            "websocket_101_count": 0,
            "websocket_failure_count": 0,
        }
        super().__init__(address, _ModelProxyHandler)

    def record_transport_event(self, key: str) -> None:
        with self._transport_lock:
            if key not in self._transport_counts:
                raise ValueError(f"unknown model transport event {key!r}")
            self._transport_counts[key] += 1

    def transport_audit(self) -> dict[str, Any]:
        with self._transport_lock:
            counts = dict(self._transport_counts)
        return {
            "schema": "hermes_phase4g8_model_transport_audit_v1",
            **counts,
        }


def _read_http_header(connection: socket.socket, *, max_bytes: int) -> bytes:
    payload = bytearray()
    while b"\r\n\r\n" not in payload:
        chunk = connection.recv(min(4096, max_bytes - len(payload)))
        if not chunk:
            raise ConnectionError("model websocket upstream closed during handshake")
        payload.extend(chunk)
        if len(payload) >= max_bytes and b"\r\n\r\n" not in payload:
            raise ValueError("model websocket handshake header is too large")
    return bytes(payload)


def _relay_bidirectional(
    client: socket.socket,
    upstream: socket.socket,
    *,
    idle_timeout_seconds: float,
) -> None:
    sockets = (client, upstream)
    while True:
        try:
            readable, _, exceptional = select.select(
                sockets,
                [],
                sockets,
                max(0.05, min(30.0, float(idle_timeout_seconds))),
            )
        except OSError:
            return
        if exceptional:
            return
        if not readable:
            continue
        for source in readable:
            try:
                chunk = source.recv(64 * 1024)
            except OSError:
                return
            if not chunk:
                return
            destination = upstream if source is client else client
            try:
                destination.sendall(chunk)
            except OSError:
                return


class Phase4G8NetworkNamespace:
    """A no-default-route worker netns with one allowlisted model proxy."""

    def __init__(self, run_id: str, upstream_base_url: str, *, timeout_seconds: float = 300.0) -> None:
        self.run_id = str(run_id or "").strip()
        if not self.run_id:
            raise ValueError("run_id is required")
        suffix = hashlib.sha256(self.run_id.encode("utf-8")).hexdigest()[:8]
        octet = int(suffix[:2], 16) % 200 + 20
        self.namespace = f"h4g8-{suffix}"
        self.host_interface = f"h4h{suffix[:7]}"[:15]
        self.namespace_interface = f"h4n{suffix[:7]}"[:15]
        self.host_ip = f"10.203.{octet}.1"
        self.namespace_ip = f"10.203.{octet}.2"
        self.cidr = f"10.203.{octet}.0/30"
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.proxy: Optional[_ModelProxyServer] = None
        self.proxy_thread: Optional[threading.Thread] = None
        self.proxy_base_url: Optional[str] = None
        self._created = False

    def start(self) -> "Phase4G8NetworkNamespace":
        if os.name == "nt" or os.geteuid() != 0:
            raise RuntimeError("Phase 4G8 network namespace requires POSIX root privileges")
        for command in ("ip", "nft", "setpriv"):
            if shutil.which(command) is None:
                raise RuntimeError(f"Phase 4G8 network namespace requires {command}")
        try:
            _run(["ip", "netns", "add", self.namespace], timeout=30)
            self._created = True
            _run(
                ["ip", "link", "add", self.host_interface, "type", "veth", "peer", "name", self.namespace_interface],
                timeout=30,
            )
            _run(["ip", "link", "set", self.namespace_interface, "netns", self.namespace], timeout=30)
            _run(["ip", "addr", "add", f"{self.host_ip}/30", "dev", self.host_interface], timeout=30)
            _run(["ip", "link", "set", self.host_interface, "up"], timeout=30)
            _run(["ip", "-n", self.namespace, "addr", "add", f"{self.namespace_ip}/30", "dev", self.namespace_interface], timeout=30)
            _run(["ip", "-n", self.namespace, "link", "set", self.namespace_interface, "up"], timeout=30)
            _run(["ip", "-n", self.namespace, "link", "set", "lo", "up"], timeout=30)
            self.proxy = _ModelProxyServer((self.host_ip, 0), self.upstream_base_url, self.timeout_seconds)
            self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
            self.proxy_thread.start()
            proxy_port = int(self.proxy.server_address[1])
            upstream_path = urllib.parse.urlsplit(self.upstream_base_url).path.rstrip("/")
            self.proxy_base_url = f"http://{self.host_ip}:{proxy_port}{upstream_path}"
            _run(["ip", "netns", "exec", self.namespace, "nft", "add", "table", "inet", "hermes"], timeout=30)
            _run(
                [
                    "ip", "netns", "exec", self.namespace, "nft", "add", "chain", "inet", "hermes", "output",
                    "{ type filter hook output priority 0; policy drop; }",
                ],
                timeout=30,
            )
            _run(
                ["ip", "netns", "exec", self.namespace, "nft", "add", "rule", "inet", "hermes", "output", "oifname", "lo", "accept"],
                timeout=30,
            )
            _run(
                [
                    "ip", "netns", "exec", self.namespace, "nft", "add", "rule", "inet", "hermes", "output",
                    "ip", "daddr", self.host_ip, "tcp", "dport", str(proxy_port), "accept",
                ],
                timeout=30,
            )
        except Exception:
            self.close()
            raise
        return self

    def wrap_argv(self, argv: list[str], *, uid: int = 65534, gid: int = 65534) -> list[str]:
        if not self._created or not self.proxy_base_url:
            raise RuntimeError("network namespace is not started")
        if not argv:
            raise ValueError("worker argv is required")
        return [
            "ip", "netns", "exec", self.namespace,
            "setpriv",
            "--bounding-set=-net_admin,-net_raw",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "--no-new-privs",
            f"--reuid={int(uid)}",
            f"--regid={int(gid)}",
            "--clear-groups",
            *argv,
        ]

    def transport_audit(self) -> dict[str, Any]:
        if self.proxy is None:
            return {
                "schema": "hermes_phase4g8_model_transport_audit_v1",
                "http_request_count": 0,
                "websocket_upgrade_attempt_count": 0,
                "websocket_101_count": 0,
                "websocket_failure_count": 0,
            }
        return self.proxy.transport_audit()

    def close(self) -> None:
        if self.proxy is not None:
            self.proxy.shutdown()
            self.proxy.server_close()
            self.proxy = None
        if self.proxy_thread is not None:
            self.proxy_thread.join(timeout=5)
            self.proxy_thread = None
        if self._created:
            subprocess.run(
                ["ip", "netns", "delete", self.namespace],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            self._created = False

    def __enter__(self) -> "Phase4G8NetworkNamespace":
        return self.start()

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()


def prepare_isolated_codex_home(
    source_home: Path,
    target_home: Path,
    *,
    proxy_base_url: str,
    model: Optional[str] = None,
    worker_uid: int = 65534,
    worker_gid: int = 65534,
) -> dict[str, Any]:
    """Copy only the active base URL/model/API key into an isolated CODEX_HOME."""

    source_home = source_home.expanduser().resolve()
    config_path = source_home / "config.toml"
    auth_path = source_home / "auth.json"
    if not config_path.is_file() or not auth_path.is_file():
        raise ValueError("source CODEX_HOME requires config.toml and auth.json")
    source_hashes = {
        "config.toml": _sha256_file(config_path),
        "auth.json": _sha256_file(auth_path),
    }
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"could not parse source CODEX_HOME: {type(exc).__name__}") from exc
    provider_name = str(config.get("model_provider") or "").strip()
    providers = config.get("model_providers") if isinstance(config.get("model_providers"), dict) else {}
    provider = providers.get(provider_name) if isinstance(providers, dict) else None
    if not isinstance(provider, dict) or not str(provider.get("base_url") or "").strip():
        raise ValueError("source Codex provider base_url is missing")
    api_key = str(auth.get("OPENAI_API_KEY") or "").strip()
    selected_model = str(model or config.get("model") or "").strip()
    reasoning_effort = str(config.get("model_reasoning_effort") or "").strip() or None
    if not api_key:
        raise ValueError("source Codex OPENAI_API_KEY is missing")
    if not selected_model:
        raise ValueError("source Codex model is missing")
    target_home.mkdir(parents=True, exist_ok=True)
    os.chmod(target_home, 0o700)
    isolated_provider = "phase4g8_proxy"
    transport_settings = _isolated_provider_transport_settings(provider)
    lines = [
        f"model = {json.dumps(selected_model)}",
        f"approval_policy = {json.dumps(PHASE4G8_CODEX_APPROVAL_POLICY)}",
        f"approvals_reviewer = {json.dumps(PHASE4G8_CODEX_APPROVAL_REVIEWER)}",
    ]
    if reasoning_effort:
        lines.append(f"model_reasoning_effort = {json.dumps(reasoning_effort)}")
    lines.extend([
        f"model_provider = {json.dumps(isolated_provider)}",
        "",
        f"[model_providers.{isolated_provider}]",
        f"name = {json.dumps('Phase 4G8 isolated model proxy')}",
        f"base_url = {json.dumps(proxy_base_url.rstrip('/'))}",
        f"wire_api = {json.dumps(str(provider.get('wire_api') or 'responses'))}",
        "requires_openai_auth = true",
    ])
    lines.extend(
        f"{key} = {json.dumps(value)}"
        for key, value in transport_settings.items()
    )
    lines.extend([
        "",
        "[auto_review]",
        f"policy = {json.dumps(PHASE4G8_CODEX_AUTO_REVIEW_POLICY.strip())}",
        "",
        "[features]",
        "guardian_approval = true",
        "",
    ])
    target_config = target_home / "config.toml"
    target_auth = target_home / "auth.json"
    target_rules = target_home / "rules"
    target_exec_policy = target_rules / "default.rules"
    target_config.write_text("\n".join(lines), encoding="utf-8")
    target_auth.write_text(json.dumps({"OPENAI_API_KEY": api_key}) + "\n", encoding="utf-8")
    target_rules.mkdir(mode=0o755, exist_ok=True)
    target_exec_policy.write_text(PHASE4G8_CODEX_EXEC_POLICY.strip() + "\n", encoding="utf-8")
    os.chmod(target_config, 0o600)
    os.chmod(target_auth, 0o600)
    os.chmod(target_rules, 0o555)
    os.chmod(target_exec_policy, 0o444)
    os.chown(target_home, int(worker_uid), int(worker_gid))
    os.chown(target_config, int(worker_uid), int(worker_gid))
    os.chown(target_auth, int(worker_uid), int(worker_gid))
    os.chown(target_rules, int(worker_uid), int(worker_gid))
    os.chown(target_exec_policy, int(worker_uid), int(worker_gid))
    approval_audit = audit_phase4g8_codex_auto_review(target_config)
    if not approval_audit["configured"]:
        raise RuntimeError("isolated Codex auto-review configuration preflight failed")
    return {
        "source_home": str(source_home),
        "target_home": str(target_home.resolve()),
        "source_hashes": source_hashes,
        "isolated_hashes": {
            "config.toml": _sha256_file(target_config),
            "auth.json": _sha256_file(target_auth),
            "rules/default.rules": _sha256_file(target_exec_policy),
        },
        "source_provider": provider_name,
        "isolated_provider": isolated_provider,
        "model": selected_model,
        "reasoning_effort": reasoning_effort,
        "provider_transport": transport_settings,
        "proxy_base_url": proxy_base_url.rstrip("/"),
        "copied_session_history": False,
        "approval": approval_audit,
    }


def _isolated_provider_transport_settings(provider: dict[str, Any]) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    supports_websockets = provider.get("supports_websockets")
    if supports_websockets is not None:
        if not isinstance(supports_websockets, bool):
            raise ValueError("source Codex supports_websockets must be boolean")
        settings["supports_websockets"] = supports_websockets
    for key, minimum, maximum in (
        ("stream_max_retries", 0, 100),
        ("websocket_connect_timeout_ms", 100, 300_000),
    ):
        value = provider.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"source Codex {key} is invalid")
        settings[key] = value
    return settings


def audit_phase4g8_codex_auto_review(config_path: Path) -> dict[str, Any]:
    """Return a secret-free audit of the effective isolated reviewer config."""

    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"could not parse isolated Codex config: {type(exc).__name__}") from exc
    auto_review = config.get("auto_review") if isinstance(config.get("auto_review"), dict) else {}
    features = config.get("features") if isinstance(config.get("features"), dict) else {}
    policy_text = str(auto_review.get("policy") or "").strip()
    exec_policy_path = config_path.parent / "rules" / "default.rules"
    try:
        exec_policy_text = exec_policy_path.read_text(encoding="utf-8").strip()
    except OSError:
        exec_policy_text = ""
    configured = bool(
        config.get("approval_policy") == PHASE4G8_CODEX_APPROVAL_POLICY
        and config.get("approvals_reviewer") == PHASE4G8_CODEX_APPROVAL_REVIEWER
        and policy_text == PHASE4G8_CODEX_AUTO_REVIEW_POLICY.strip()
        and exec_policy_text == PHASE4G8_CODEX_EXEC_POLICY.strip()
        and features.get("guardian_approval") is True
    )
    return {
        "configured": configured,
        "policy": str(config.get("approval_policy") or ""),
        "reviewer": str(config.get("approvals_reviewer") or ""),
        "auto_review_policy_version": (
            PHASE4G8_CODEX_AUTO_REVIEW_POLICY_VERSION
            if policy_text == PHASE4G8_CODEX_AUTO_REVIEW_POLICY.strip()
            else None
        ),
        "auto_review_policy_sha256": (
            hashlib.sha256(policy_text.encode("utf-8")).hexdigest()
            if policy_text
            else None
        ),
        "guardian_approval_enabled": features.get("guardian_approval") is True,
        "exec_policy_version": (
            PHASE4G8_CODEX_EXEC_POLICY_VERSION
            if exec_policy_text == PHASE4G8_CODEX_EXEC_POLICY.strip()
            else None
        ),
        "exec_policy_sha256": (
            hashlib.sha256(exec_policy_text.encode("utf-8")).hexdigest()
            if exec_policy_text
            else None
        ),
    }


def load_codex_model_source(source_home: Path, *, model: Optional[str] = None) -> dict[str, Any]:
    """Load a real model source for providers without returning secrets in its summary."""

    source_home = source_home.expanduser().resolve()
    config_path = source_home / "config.toml"
    auth_path = source_home / "auth.json"
    if not config_path.is_file() or not auth_path.is_file():
        raise ValueError("source CODEX_HOME requires config.toml and auth.json")
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"could not parse source CODEX_HOME: {type(exc).__name__}") from exc
    provider_name = str(config.get("model_provider") or "").strip()
    providers = config.get("model_providers") if isinstance(config.get("model_providers"), dict) else {}
    provider = providers.get(provider_name) if isinstance(providers, dict) else None
    base_url = str(provider.get("base_url") or "").strip() if isinstance(provider, dict) else ""
    api_key = str(auth.get("OPENAI_API_KEY") or "").strip() if isinstance(auth, dict) else ""
    selected_model = str(model or config.get("model") or "").strip()
    reasoning_effort = str(config.get("model_reasoning_effort") or "").strip() or None
    if not provider_name or not base_url or not api_key or not selected_model:
        raise ValueError("source CODEX_HOME model source is incomplete")
    return {
        "provider_name": "custom",
        "display_provider": f"codex:{provider_name}",
        "model": selected_model,
        "reasoning_effort": reasoning_effort,
        "explicit_base_url": base_url.rstrip("/"),
        "explicit_api_key": api_key,
        "source_hashes": {
            "config.toml": _sha256_file(config_path),
            "auth.json": _sha256_file(auth_path),
        },
        "summary": {
            "source": "codex_config",
            "provider": provider_name,
            "model": selected_model,
            "reasoning_effort": reasoning_effort,
            "base_url_sha256": hashlib.sha256(base_url.rstrip("/").encode("utf-8")).hexdigest(),
        },
    }


def prepare_worker_workspace(path: Path, *, worker_uid: int = 65534, worker_gid: int = 65534) -> None:
    """Make a workspace owner-only for one unprivileged worker identity."""

    path = path.resolve()
    if not path.is_dir():
        raise ValueError("worker workspace must be a directory")
    for item in [path, *path.rglob("*")]:
        try:
            os.chown(item, int(worker_uid), int(worker_gid), follow_symlinks=False)
            if item.is_symlink():
                continue
            mode = item.stat(follow_symlinks=False).st_mode
            if item.is_dir():
                os.chmod(item, 0o700, follow_symlinks=False)
            elif item.is_file():
                os.chmod(item, 0o700 if mode & 0o111 else 0o600, follow_symlinks=False)
        except FileNotFoundError:
            continue


def make_phase4g8_evaluator_lane(config: dict[str, Any]):
    """Create the trusted local lane that runs the protected official evaluator."""

    from hermes_cli.worker_lanes import WorkerLane, normalize_lane_name

    name = normalize_lane_name(str(config.get("name") or "phase4g8-evaluator"))
    spec_path = Path(str(config.get("spec_path") or "")).expanduser().resolve()
    run_id = str(config.get("run_id") or "").strip()
    expected_environment_sha256 = str(config.get("expected_environment_sha256") or "").strip()
    heartbeat_interval_seconds = float(config.get("heartbeat_interval_seconds") or 10.0)
    if not spec_path.is_file() or not run_id:
        raise ValueError("Phase 4G8 evaluator lane requires spec_path and run_id")
    if heartbeat_interval_seconds <= 0:
        raise ValueError("Phase 4G8 evaluator heartbeat interval must be positive")
    if expected_environment_sha256 and len(expected_environment_sha256) != 64:
        raise ValueError("Phase 4G8 evaluator expected environment SHA-256 is invalid")

    def spawn(task: Any, workspace: str, *, board: Optional[str] = None) -> Optional[int]:
        from hermes_cli import kanban_db as kb

        command = [
            sys.executable,
            "-m",
            "hermes_cli.phase4g8_evaluator",
            "--task-id",
            task.id,
            "--workspace",
            workspace,
            "--spec",
            str(spec_path),
            "--run-id",
            run_id,
            "--heartbeat-interval",
            str(heartbeat_interval_seconds),
        ]
        if task.current_run_id is not None:
            command.extend(["--task-run-id", str(task.current_run_id)])
        resolved_board = kb._normalize_board_slug(board) or kb.get_current_board()
        command.extend(["--board", resolved_board])
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {
                "PATH", "HOME", "HERMES_HOME", "HERMES_KANBAN_HOME", "HERMES_KANBAN_DB", "PYTHONPATH",
                "PIP_INDEX_URL", "PIP_TRUSTED_HOST",
            }
        }
        package_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = (
            package_root + os.pathsep + env["PYTHONPATH"]
            if env.get("PYTHONPATH") else package_root
        )
        if expected_environment_sha256:
            env["HERMES_PHASE4G8_EXPECTED_ENVIRONMENT_SHA256"] = expected_environment_sha256
        env[PROCESS_OWNER_ENV] = run_id
        log_path = kb.worker_log_path(task.id, board=board)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("ab")
        try:
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            log_handle.close()
        return process.pid

    return WorkerLane(
        name=name,
        kind="phase4g8_evaluator",
        description="Protected fixed-revision Phase 4G8 official evaluator",
        spawn_fn=spawn,
        success_policy="block_for_review",
        max_concurrency=1,
        source="phase4g8",
        config={
            "type": "phase4g8_evaluator",
            "spec_path": str(spec_path),
            "run_id": run_id,
            "heartbeat_interval_seconds": heartbeat_interval_seconds,
            "expected_environment_sha256": expected_environment_sha256,
        },
    )


def verify_codex_source_unchanged(source_home: Path, expected_hashes: dict[str, str]) -> bool:
    source_home = source_home.expanduser().resolve()
    return all(
        (source_home / name).is_file() and _sha256_file(source_home / name) == expected
        for name, expected in expected_hashes.items()
    )


def build_phase4g8_run_report(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    instance_id: str,
    evaluator_result: dict[str, Any],
    process_boundaries: dict[str, bool],
    metrics: Optional[dict[str, Any]] = None,
    credential_scan_hits: int = 0,
    source_config_unchanged: bool = True,
) -> dict[str, Any]:
    from hermes_cli import kanban_runtime_decision as rd
    from hermes_cli import kanban_runtime_kernel as rk

    if evaluator_result.get("schema") != EVALUATOR_RESULT_SCHEMA:
        raise ValueError("official evaluator result has an invalid schema")
    status = rk.status_runtime_job(conn, job_id)
    consistency = rk.check_runtime_consistency(conn, job_id, write_events=False)
    chain = rd.validate_decision_context_chain(conn, job_id)
    duplicate_terminal = conn.execute(
        """
        SELECT node_id, task_id, run_id, COUNT(*) AS count
          FROM execution_events
         WHERE job_id = ? AND event_type IN ('node_completed', 'node_failed', 'node_blocked')
         GROUP BY node_id, task_id, run_id HAVING COUNT(*) > 1
        """,
        (job_id,),
    ).fetchall()
    duplicate_ledger = conn.execute(
        """
        SELECT goal_item_id, evidence_ref, COUNT(*) AS count
          FROM progress_ledger
         WHERE job_id = ?
         GROUP BY goal_item_id, evidence_ref HAVING COUNT(*) > 1
        """,
        (job_id,),
    ).fetchall()
    fallback_count = int(conn.execute(
        "SELECT COUNT(*) FROM decision_segment_entries WHERE job_id = ? AND entry_type = 'compaction_fallback'",
        (job_id,),
    ).fetchone()[0])
    required_items = [item for item in status["goal_items"] if item["required"]]
    independently_verified = {
        row["goal_item_id"]
        for row in status["progress_ledger"]
        if row["satisfaction"] == "full" and row["verification_state"] == "independently_verified"
    }
    premature_done = status["job"]["state"] == "done" and any(
        item["verifier_required"] and item["id"] not in independently_verified and item["state"] != "waived"
        for item in required_items
    )
    boundary_failures = sorted(key for key, passed in process_boundaries.items() if passed is not True)
    runtime_failures: list[str] = []
    if consistency["violation_count"]:
        runtime_failures.append("consistency_violations")
    if consistency["warning_count"]:
        runtime_failures.append("consistency_warnings")
    if duplicate_terminal:
        runtime_failures.append("duplicate_terminal_facts")
    if duplicate_ledger:
        runtime_failures.append("duplicate_ledger_facts")
    if fallback_count:
        runtime_failures.append("compaction_fallback_used")
    if chain.get("status") != "valid":
        runtime_failures.append("checkpoint_chain_invalid")
    if premature_done:
        runtime_failures.append("premature_done_without_independent_verification")
    if boundary_failures:
        runtime_failures.append("missing_process_boundaries")
    if credential_scan_hits:
        runtime_failures.append("credential_scan_hits")
    if not source_config_unchanged:
        runtime_failures.append("source_codex_config_changed")
    runtime_passed = not runtime_failures
    official_resolved = evaluator_result.get("resolved") is True
    resource_exhausted = bool((metrics or {}).get("resource_exhausted"))
    capability_passed = runtime_passed and official_resolved and status["job"]["state"] == "done"
    classification = (
        "resolved"
        if capability_passed
        else "runtime-correct/resource-exhausted"
        if runtime_passed and resource_exhausted
        else "runtime-correct/task-failed"
        if runtime_passed and not official_resolved
        else "runtime-correct/runtime-incomplete"
        if runtime_passed
        else "runtime-correctness-failed"
    )
    return {
        "schema": RUN_REPORT_SCHEMA,
        "instance_id": instance_id,
        "job_id": job_id,
        "classification": classification,
        "runtime_validation": {
            "passed": runtime_passed,
            "failures": runtime_failures,
            "consistency": {
                "status": consistency["status"],
                "violation_count": consistency["violation_count"],
                "warning_count": consistency["warning_count"],
            },
            "checkpoint_chain": chain,
            "compaction_fallback_count": fallback_count,
            "duplicate_terminal_fact_count": len(duplicate_terminal),
            "duplicate_ledger_fact_count": len(duplicate_ledger),
            "premature_done": premature_done,
            "process_boundaries": process_boundaries,
            "credential_scan_hits": int(credential_scan_hits),
            "source_config_unchanged": bool(source_config_unchanged),
        },
        "capability_validation": {
            "passed": capability_passed,
            "official_resolved": official_resolved,
            "runtime_job_state": status["job"]["state"],
            "fail_to_pass": evaluator_result.get("fail_to_pass"),
            "pass_to_pass": evaluator_result.get("pass_to_pass"),
        },
        "metrics": metrics or {},
        "generated_at": int(time.time()),
    }


def aggregate_phase4g8_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    if len(reports) != 3 or any(report.get("schema") != RUN_REPORT_SCHEMA for report in reports):
        raise ValueError("Phase 4G8 aggregate requires exactly three valid run reports")
    runtime_passed = all(report["runtime_validation"]["passed"] for report in reports)
    capability_passed = all(report["capability_validation"]["passed"] for report in reports)
    return {
        "schema": AGGREGATE_REPORT_SCHEMA,
        "runtime_validation": {
            "passed": runtime_passed,
            "passed_instances": sum(bool(report["runtime_validation"]["passed"]) for report in reports),
            "total_instances": len(reports),
        },
        "capability_validation": {
            "passed": capability_passed,
            "resolved_instances": sum(bool(report["capability_validation"]["passed"]) for report in reports),
            "total_instances": len(reports),
        },
        "instances": [
            {
                "instance_id": report["instance_id"],
                "classification": report["classification"],
                "runtime_passed": report["runtime_validation"]["passed"],
                "capability_passed": report["capability_validation"]["passed"],
            }
            for report in reports
        ],
        "generated_at": int(time.time()),
    }


@dataclass(frozen=True)
class Phase4G8Layout:
    root: Path
    instance_id: str

    @property
    def instance_root(self) -> Path:
        return self.root / self.instance_id

    @property
    def protected(self) -> Path:
        return self.instance_root / "protected"

    @property
    def worker(self) -> Path:
        return self.instance_root / "worker"

    @property
    def reports(self) -> Path:
        return self.instance_root / "reports"

    def prepare(self) -> None:
        for path in (
            self.protected,
            self.worker,
            self.reports,
            self.instance_root / "home",
            self.instance_root / "hermes-home",
            self.instance_root / "codex-home",
            self.instance_root / "artifacts",
            self.instance_root / "service",
        ):
            path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.protected, 0o700)


def load_qualification_spec(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_qualification_spec(payload)
    return payload


def validate_qualification_spec(spec: dict[str, Any]) -> None:
    if not isinstance(spec, dict) or spec.get("schema") != QUALIFICATION_SPEC_SCHEMA:
        raise ValueError(f"qualification spec schema must be {QUALIFICATION_SPEC_SCHEMA}")
    for key in ("instance_id", "dataset_revision", "repository", "base_commit", "srs"):
        if not str(spec.get(key) or "").strip():
            raise ValueError(f"qualification spec requires {key}")
    source = spec.get("source")
    if not isinstance(source, dict) or not str(source.get("local_mirror") or "").strip():
        raise ValueError("qualification spec requires source.local_mirror")
    gold = spec.get("gold")
    if not isinstance(gold, dict) or not str(gold.get("patch_path") or "").strip():
        raise ValueError("qualification spec requires gold.patch_path")
    evaluator = spec.get("evaluator")
    if not isinstance(evaluator, dict):
        raise ValueError("qualification spec requires evaluator")
    argv = evaluator.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(value, str) or not value for value in argv):
        raise ValueError("evaluator.argv must be a non-empty string array")
    if evaluator.get("env") is not None and not isinstance(evaluator["env"], dict):
        raise ValueError("evaluator.env must be an object")
    timeout = int(evaluator.get("timeout_seconds") or 0)
    if timeout < 1:
        raise ValueError("evaluator.timeout_seconds must be positive")
    requirements = spec.get("public_requirements") or []
    if not isinstance(requirements, list) or any(not isinstance(item, str) for item in requirements):
        raise ValueError("public_requirements must be a string array")
    worker_environment = spec.get("worker_environment")
    if worker_environment is not None:
        if not isinstance(worker_environment, dict):
            raise ValueError("worker_environment must be an object")
        renderer_argv = worker_environment.get("renderer_argv")
        if (
            not isinstance(renderer_argv, list)
            or not renderer_argv
            or any(not isinstance(value, str) or not value for value in renderer_argv)
        ):
            raise ValueError("worker_environment.renderer_argv must be a non-empty string array")
        if worker_environment.get("env") is not None and not isinstance(worker_environment["env"], dict):
            raise ValueError("worker_environment.env must be an object")


def run_oracle_qualification(
    spec: dict[str, Any],
    *,
    output_root: Path,
    min_free_bytes: int = 8 * 1024 * 1024 * 1024,
) -> dict[str, Any]:
    validate_qualification_spec(spec)
    layout = Phase4G8Layout(output_root, str(spec["instance_id"]))
    layout.prepare()
    _require_disk_margin(layout.instance_root, min_free_bytes)
    mirror = Path(spec["source"]["local_mirror"]).expanduser().resolve()
    gold_patch = Path(spec["gold"]["patch_path"]).expanduser().resolve()
    if not (mirror / ".git").exists():
        raise ValueError("source.local_mirror must be a local git repository")
    if not gold_patch.is_file():
        raise ValueError("gold patch does not exist")

    base_workspace = layout.protected / "base"
    gold_workspace = layout.protected / "gold"
    for path in (base_workspace, gold_workspace):
        if path.exists():
            shutil.rmtree(path)
        _run(["git", "clone", "--no-hardlinks", "--quiet", str(mirror), str(path)], timeout=300)
        _run(["git", "checkout", "--quiet", str(spec["base_commit"])], cwd=path, timeout=120)
    exact_base = _run(["git", "rev-parse", "HEAD"], cwd=base_workspace, timeout=30)["stdout"].strip()
    if exact_base != str(spec["base_commit"]):
        raise ValueError(f"base commit mismatch: expected {spec['base_commit']}, got {exact_base}")

    base_result = _run_evaluator(spec, base_workspace)
    _run(["git", "apply", "--binary", str(gold_patch)], cwd=gold_workspace, timeout=120)
    gold_result = _run_evaluator(spec, gold_workspace)
    oracle = _validate_oracle_results(base_result, gold_result)
    gold_hash = _sha256_file(gold_patch)
    report = {
        "schema": QUALIFICATION_REPORT_SCHEMA,
        "instance_id": spec["instance_id"],
        "dataset_revision": spec["dataset_revision"],
        "repository": spec["repository"],
        "base_commit": exact_base,
        "gold_patch_sha256": gold_hash,
        "base_result": base_result,
        "gold_result": gold_result,
        "oracle": oracle,
        "resource": {
            "base_checkout_bytes": _tree_size(base_workspace),
            "gold_checkout_bytes": _tree_size(gold_workspace),
            "free_bytes_after": shutil.disk_usage(layout.instance_root).free,
        },
        "qualified_at": int(time.time()),
    }
    protected_report = layout.protected / "qualification-report.json"
    _write_json(protected_report, report, mode=0o600)
    locked = _locked_task_manifest(spec, report)
    _write_json(layout.worker / "locked-task.json", locked, mode=0o644)
    _assert_gold_isolation(layout.worker, gold_patch, spec)
    return {"report": report, "locked_manifest": locked, "layout": str(layout.instance_root)}


def evaluate_fault_trigger(
    conn: sqlite3.Connection,
    job_id: str,
    trigger: str,
    *,
    node_key: Optional[str] = None,
    now: Optional[int] = None,
) -> dict[str, Any]:
    if trigger not in FAULT_TRIGGERS:
        raise ValueError(f"unsupported fault trigger {trigger!r}")
    node_filter = " AND n.node_key = ?" if node_key else ""
    params: list[Any] = [job_id]
    if node_key:
        params.append(node_key)
    if trigger == "worker_running":
        row = conn.execute(
            """
            SELECT n.id AS node_id, n.node_key, n.latest_task_id, n.latest_run_id, nm.id AS materialization_id
              FROM execution_nodes n
              JOIN node_materializations nm ON nm.node_id = n.id AND nm.task_id = n.latest_task_id
             WHERE n.job_id = ? AND n.state = 'running' AND nm.status = 'running'
            """ + node_filter + " ORDER BY nm.attempt DESC LIMIT 1",
            params,
        ).fetchone()
        return _trigger_result(trigger, row, "no running worker materialization")
    if trigger == "receipt_before_ingest":
        row = conn.execute(
            """
            SELECT n.id AS node_id, n.node_key, n.latest_task_id, n.latest_run_id, nm.id AS materialization_id
              FROM execution_nodes n
              JOIN node_materializations nm ON nm.node_id = n.id AND nm.task_id = n.latest_task_id
             JOIN tasks t ON t.id = n.latest_task_id
             WHERE n.job_id = ? AND n.state = 'running' AND t.status IN ('done', 'blocked')
               AND NOT EXISTS (
                   SELECT 1 FROM progress_ledger pl
                    WHERE pl.node_id = n.id
                      AND pl.evidence_ref = 'node:' || n.id || ':materialization:' || nm.id
               )
               AND NOT EXISTS (
                   SELECT 1 FROM execution_events ee
                    WHERE ee.node_id = n.id AND ee.task_id = t.id
                      AND ee.event_type IN ('node_completed', 'node_failed', 'node_blocked')
               )
            """ + node_filter + " ORDER BY nm.attempt DESC LIMIT 1",
            params,
        ).fetchone()
        return _trigger_result(trigger, row, "terminal receipt is not waiting for ingest")
    current = int(now if now is not None else time.time())
    row = conn.execute(
        """
        SELECT id AS job_id, advance_lock, claim_expires_at
          FROM runtime_jobs
         WHERE id = ? AND advance_lock IS NOT NULL AND claim_expires_at <= ?
        """,
        (job_id, current),
    ).fetchone()
    return _trigger_result(trigger, row, "runtime lease has not expired")


def terminate_owned_process_group(
    pid: int,
    *,
    run_id: str,
    hard: bool = False,
) -> dict[str, Any]:
    """Signal only a process group explicitly marked as owned by this run."""

    if os.name == "nt":
        raise RuntimeError("Phase 4G8 process-group fault injection requires POSIX")
    target_pid = int(pid)
    if target_pid <= 1 or target_pid == os.getpid():
        raise ValueError("refusing to signal unsafe process id")
    expected = str(run_id or "").strip()
    if not expected:
        raise ValueError("run_id is required")
    try:
        pgid = os.getpgid(target_pid)
    except ProcessLookupError as exc:
        raise ValueError("target process does not exist") from exc
    if pgid != target_pid:
        raise ValueError("target process is not the process-group leader")
    environ_path = Path(f"/proc/{target_pid}/environ")
    try:
        environ = environ_path.read_bytes().split(b"\0")
    except OSError as exc:
        raise ValueError("cannot verify target process ownership") from exc
    marker = f"{PROCESS_OWNER_ENV}={expected}".encode("utf-8")
    if marker not in environ:
        raise ValueError("target process is not owned by this Phase 4G8 run")
    signum = signal.SIGKILL if hard else signal.SIGTERM
    os.killpg(pgid, signum)
    return {
        "pid": target_pid,
        "process_group_id": pgid,
        "run_id": expected,
        "signal": signal.Signals(signum).name,
    }


def runtime_fact_counts(
    conn: sqlite3.Connection,
    job_id: str,
    node_id: str,
    *,
    materialization_id: Optional[str] = None,
) -> dict[str, int]:
    """Return committed fact counts used after receipt-before-ingest recovery."""

    if materialization_id is not None:
        materialization = conn.execute(
            "SELECT task_id FROM node_materializations WHERE job_id = ? AND node_id = ? AND id = ?",
            (job_id, node_id, materialization_id),
        ).fetchone()
        if materialization is None:
            raise ValueError("unknown node materialization for runtime fact counts")
        evidence_ref = f"node:{node_id}:materialization:{materialization_id}"
        task_id = str(materialization["task_id"])
        return {
            "ledger": int(conn.execute(
                "SELECT COUNT(*) FROM progress_ledger WHERE job_id = ? AND node_id = ? AND evidence_ref = ?",
                (job_id, node_id, evidence_ref),
            ).fetchone()[0]),
            "terminal_events": int(conn.execute(
                """
                SELECT COUNT(*) FROM execution_events
                 WHERE job_id = ? AND node_id = ? AND task_id = ?
                   AND event_type IN ('node_completed', 'node_failed', 'node_blocked')
                """,
                (job_id, node_id, task_id),
            ).fetchone()[0]),
            "terminal_materializations": int(conn.execute(
                """
                SELECT COUNT(*) FROM node_materializations
                 WHERE job_id = ? AND node_id = ? AND id = ?
                   AND status IN ('succeeded', 'failed', 'blocked')
                """,
                (job_id, node_id, materialization_id),
            ).fetchone()[0]),
        }
    return {
        "ledger": int(conn.execute(
            "SELECT COUNT(*) FROM progress_ledger WHERE job_id = ? AND node_id = ?",
            (job_id, node_id),
        ).fetchone()[0]),
        "terminal_events": int(conn.execute(
            """
            SELECT COUNT(*) FROM execution_events
             WHERE job_id = ? AND node_id = ?
               AND event_type IN ('node_completed', 'node_failed', 'node_blocked')
            """,
            (job_id, node_id),
        ).fetchone()[0]),
        "terminal_materializations": int(conn.execute(
            """
            SELECT COUNT(*) FROM node_materializations
             WHERE job_id = ? AND node_id = ? AND status IN ('succeeded', 'failed', 'blocked')
            """,
            (job_id, node_id),
        ).fetchone()[0]),
    }


def _run_evaluator(
    spec: dict[str, Any],
    workspace: Path,
    *,
    extra_env: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    evaluator = spec["evaluator"]
    replacements = {
        "workspace": str(workspace),
        "instance_id": str(spec["instance_id"]),
        "base_commit": str(spec["base_commit"]),
    }
    argv = [value.format(**replacements) for value in evaluator["argv"]]
    env = os.environ.copy()
    for key, value in (evaluator.get("env") or {}).items():
        env[str(key)] = str(value).format(**replacements)
    for key, value in (extra_env or {}).items():
        env[str(key)] = str(value)
    started = time.monotonic()
    completed = _run(argv, cwd=workspace, env=env, timeout=int(evaluator["timeout_seconds"]))
    try:
        result = json.loads(completed["stdout"])
    except json.JSONDecodeError as exc:
        raise ValueError("evaluator stdout must be one JSON object") from exc
    if not isinstance(result, dict) or result.get("schema") != EVALUATOR_RESULT_SCHEMA:
        raise ValueError(f"evaluator result schema must be {EVALUATOR_RESULT_SCHEMA}")
    for key in ("fail_to_pass", "pass_to_pass"):
        section = result.get(key)
        if not isinstance(section, dict):
            raise ValueError(f"evaluator result requires {key}")
        for field in ("passed", "failed", "total"):
            if not isinstance(section.get(field), int) or section[field] < 0:
                raise ValueError(f"evaluator result {key}.{field} must be a non-negative integer")
        if section["passed"] + section["failed"] != section["total"]:
            raise ValueError(f"evaluator result {key} counts do not add up")
    result["wall_time_seconds"] = round(time.monotonic() - started, 3)
    result["stdout_sha256"] = hashlib.sha256(completed["stdout"].encode("utf-8")).hexdigest()
    return result


def _validate_oracle_results(base: dict[str, Any], gold: dict[str, Any]) -> dict[str, Any]:
    base_fail = base["fail_to_pass"]
    base_pass = base["pass_to_pass"]
    gold_fail = gold["fail_to_pass"]
    gold_pass = gold["pass_to_pass"]
    checks = {
        "base_fail_to_pass_fails": base_fail["failed"] > 0,
        "base_pass_to_pass_passes": base_pass["failed"] == 0,
        "gold_fail_to_pass_passes": gold_fail["failed"] == 0 and gold_fail["passed"] == gold_fail["total"],
        "gold_pass_to_pass_passes": gold_pass["failed"] == 0 and gold_pass["passed"] == gold_pass["total"],
        "gold_resolved": gold.get("resolved") is True,
    }
    base_environment = base.get("environment_fingerprint")
    gold_environment = gold.get("environment_fingerprint")
    if base_environment is not None or gold_environment is not None:
        checks["base_gold_environment_match"] = bool(
            isinstance(base_environment, dict)
            and isinstance(gold_environment, dict)
            and base_environment.get("sha256")
            and base_environment.get("sha256") == gold_environment.get("sha256")
        )
    failed = [key for key, value in checks.items() if not value]
    if failed:
        raise ValueError("oracle qualification failed: " + ", ".join(failed))
    return {"status": "qualified", "checks": checks}


def _locked_task_manifest(spec: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    manifest = {
        "schema": LOCKED_MANIFEST_SCHEMA,
        "instance_id": spec["instance_id"],
        "dataset_revision": spec["dataset_revision"],
        "repository": spec["repository"],
        "base_commit": report["base_commit"],
        "srs": spec["srs"],
        "public_requirements": spec.get("public_requirements") or [],
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return manifest


def _assert_gold_isolation(worker_root: Path, gold_patch: Path, spec: dict[str, Any]) -> None:
    forbidden = {
        str(gold_patch),
        gold_patch.name,
        gold_patch.read_text(encoding="utf-8", errors="replace"),
    }
    text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in worker_root.rglob("*")
        if path.is_file()
    )
    for value in forbidden:
        if value and value in text:
            raise ValueError("gold patch leaked into worker-visible qualification output")


def _run(
    argv: list[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
    timeout: int,
) -> dict[str, Any]:
    if not isinstance(argv, list) or not argv or any(not isinstance(value, str) or not value for value in argv):
        raise ValueError("command must be a non-empty argv array")
    completed = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        error = redact_sensitive_text(completed.stderr or completed.stdout or "command failed")
        raise RuntimeError(f"command failed with exit code {completed.returncode}: {error[:2000]}")
    return {"stdout": completed.stdout, "stderr": completed.stderr, "returncode": completed.returncode}


def _trigger_result(trigger: str, row: Optional[sqlite3.Row], waiting_reason: str) -> dict[str, Any]:
    return {
        "trigger": trigger,
        "ready": row is not None,
        "facts": dict(row) if row is not None else {},
        "waiting_reason": None if row is not None else waiting_reason,
    }


def _require_disk_margin(path: Path, min_free_bytes: int) -> None:
    free = shutil.disk_usage(path).free
    if free < int(min_free_bytes):
        raise RuntimeError(f"insufficient disk margin: required {int(min_free_bytes)} free bytes, found {free}")


def _tree_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any], *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)
