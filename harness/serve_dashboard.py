#!/usr/bin/env python3
"""Serve the Bayview dashboard with in-place Cekura refresh support.

The dashboard remains a static HTML app, but this local server adds two API
endpoints:

    GET  /api/report   returns the current report.json
    POST /api/refresh  fetches the latest Cekura result and returns fresh data

Use the printed URL with refresh_token=... when serving through ngrok.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from generate_dashboard import (
    CEKURA_API_BASE,
    DEFAULT_INPUT,
    ROOT,
    cekura_get,
    generate_dashboard,
    load_env_value,
    render_html,
)

DEFAULT_OUT = ROOT / "harness" / "runs" / "latest-cekura"
DEFAULT_PORT = 8765


def env_value(name: str) -> str:
    return os.environ.get(name) or load_env_value(name)


def _lookup_scenario_ids(scenario_names: list[str], agent_id: int) -> dict[str, int]:
    """Fetch scenarios from Cekura and return a name→id map for the requested names."""
    api_key = env_value("CEKURA_API_KEY")
    name_set = set(scenario_names)
    found: dict[str, int] = {}
    page = 1
    while True:
        query = urllib.parse.urlencode({"agent_id": agent_id, "page": page})
        data = cekura_get(f"/test_framework/v1/scenarios/?{query}", api_key)
        for scenario in data.get("results", []):
            name = scenario.get("name", "")
            sid = scenario.get("id")
            if name in name_set and sid is not None:
                found[name] = int(sid)
        if not data.get("next"):
            break
        page += 1
    return found


def normalize_ngrok_domain(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        parsed = urlparse(text)
        return parsed.netloc.rstrip("/")
    return text.strip("/")


def public_url_for(domain: str, token: str) -> str:
    normalized = normalize_ngrok_domain(domain)
    if not normalized:
        return ""
    return f"https://{normalized}/?refresh_token={token}"


@dataclass
class DashboardState:
    out_dir: Path
    input_path: Path
    title: str
    cekura_agent_id: int
    refresh_token: str
    lock: threading.Lock

    @property
    def index_path(self) -> Path:
        return self.out_dir / "index.html"

    @property
    def report_path(self) -> Path:
        return self.out_dir / "report.json"

    def ensure_files(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if self.report_path.exists():
            model = json.loads(self.report_path.read_text(encoding="utf-8"))
            self.index_path.write_text(render_html(model), encoding="utf-8")
            return
        generate_dashboard(
            input_path=self.input_path,
            out_dir=self.out_dir,
            title=self.title,
            write_html=True,
        )

    def read_report(self) -> dict[str, Any]:
        self.ensure_files()
        return json.loads(self.report_path.read_text(encoding="utf-8"))

    def refresh(self) -> dict[str, Any]:
        with self.lock:
            write_html = not self.index_path.exists()
            return generate_dashboard(
                input_path=self.input_path,
                out_dir=self.out_dir,
                title=self.title,
                cekura_result_id="latest",
                cekura_agent_id=self.cekura_agent_id,
                write_html=write_html,
            )


class DashboardHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler: type[SimpleHTTPRequestHandler], state: DashboardState):
        super().__init__(server_address, handler)
        self.state = state


class DashboardHandler(SimpleHTTPRequestHandler):
    server: DashboardHTTPServer

    def end_headers(self) -> None:
        parsed_path = urlparse(self.path).path
        if parsed_path.startswith("/api/") or parsed_path.endswith("report.json"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/report":
            try:
                self.send_json({"ok": True, "model": self.server.state.read_report()})
            except Exception as exc:  # noqa: BLE001 - user-facing local server
                self.send_json({"ok": False, "error": str(exc)}, status=500)
            return
        if parsed.path == "/api/heal-status":
            self._serve_heal_status()
            return
        super().do_GET()

    def _serve_heal_status(self) -> None:
        status_path = ROOT / "harness" / "runs" / "webhook-status.json"
        try:
            if status_path.exists():
                data = json.loads(status_path.read_text(encoding="utf-8"))
                self.send_json({"ok": True, "status": data})
            else:
                self.send_json({"ok": True, "status": None})
        except Exception as exc:  # noqa: BLE001 - user-facing local server
            self.send_json({"ok": False, "error": str(exc)}, status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/trigger-heal":
            self._handle_trigger_heal()
            return
        if parsed.path != "/api/refresh":
            self.send_error(404)
            return
        if not self.authorized(parsed):
            self.send_json({"ok": False, "error": "Refresh token missing or invalid."}, status=401)
            return
        try:
            model = self.server.state.refresh()
            self.send_json({"ok": True, "model": model})
        except Exception as exc:  # noqa: BLE001 - user-facing local server
            self.send_json({"ok": False, "error": str(exc)}, status=500)

    def _handle_trigger_heal(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length else b""
        try:
            body = json.loads(body_bytes) if body_bytes else {}
        except Exception:
            self.send_json({"ok": False, "error": "Invalid JSON"}, status=400)
            return

        scenario_names: list[str] = body.get("scenario_names", [])
        if not scenario_names:
            self.send_json({"ok": False, "error": "scenario_names is required"}, status=400)
            return

        try:
            id_map = _lookup_scenario_ids(scenario_names, self.server.state.cekura_agent_id)
        except Exception as exc:  # noqa: BLE001 - user-facing local server
            self.send_json({"ok": False, "error": f"Cekura lookup failed: {exc}"}, status=502)
            return

        enqueued = []
        errors = []
        for name in scenario_names:
            sid = id_map.get(name)
            if sid is None:
                errors.append(f"Scenario not found in Cekura: {name!r}")
                continue
            try:
                payload = json.dumps({"scenario_id": sid, "scenario_name": name}).encode()
                req = urllib.request.Request(
                    "http://127.0.0.1:8888/internal/enqueue",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    result = json.loads(resp.read())
                    if result.get("ok"):
                        enqueued.append({"scenario_id": sid, "scenario_name": name})
            except Exception as exc:  # noqa: BLE001 - user-facing local server
                errors.append(f"Failed to enqueue {name!r}: {exc}")

        self.send_json({"ok": True, "enqueued": enqueued, "errors": errors})

    def authorized(self, parsed: Any) -> bool:
        expected = self.server.state.refresh_token
        supplied = self.headers.get("X-Dashboard-Refresh-Token", "")
        if not supplied:
            supplied = parse_qs(parsed.query).get("refresh_token", [""])[0]
        return bool(supplied) and secrets.compare_digest(supplied, expected)

    def send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the Bayview dashboard with manual Cekura refresh.")
    parser.add_argument("--host", default="127.0.0.1", help="Host for the local dashboard server.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port for the local dashboard server.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Dashboard run directory to serve.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Fallback input for first-time index generation.")
    parser.add_argument("--title", default="Bayview Pharmacy Self-Improvement Harness")
    parser.add_argument("--cekura-agent-id", type=int, default=18021, help="Cekura agent ID used for latest-result refresh.")
    parser.add_argument(
        "--refresh-token",
        default="",
        help="Token required by POST /api/refresh. Defaults to DASHBOARD_REFRESH_TOKEN or a generated token.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    token = args.refresh_token or env_value("DASHBOARD_REFRESH_TOKEN") or secrets.token_urlsafe(24)
    state = DashboardState(
        out_dir=args.out.resolve(),
        input_path=args.input.resolve(),
        title=args.title,
        cekura_agent_id=args.cekura_agent_id,
        refresh_token=token,
        lock=threading.Lock(),
    )
    state.ensure_files()

    handler = partial(DashboardHandler, directory=str(state.out_dir))
    server = DashboardHTTPServer((args.host, args.port), handler, state)
    local_url = f"http://{args.host}:{args.port}/?refresh_token={token}"
    ngrok_url = public_url_for(env_value("NGROK_DOMAIN"), token)

    print(f"Serving dashboard from: {state.out_dir}")
    print(f"Local URL: {local_url}")
    if ngrok_url:
        print(f"Ngrok URL: {ngrok_url}")
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
