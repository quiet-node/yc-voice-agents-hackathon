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
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
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
        if parsed.path == "/api/transcripts":
            self._serve_transcripts()
            return
        if parsed.path == "/api/scenarios":
            self._serve_scenarios()
            return
        super().do_GET()

    def _serve_scenarios(self) -> None:
        """Return all Cekura scenarios for the configured agent."""
        api_key = env_value("CEKURA_API_KEY")
        agent_id = self.server.state.cekura_agent_id
        scenarios: list[dict[str, Any]] = []
        try:
            page = 1
            while True:
                query = urllib.parse.urlencode({"agent_id": agent_id, "page": page})
                data = cekura_get(f"/test_framework/v1/scenarios/?{query}", api_key)
                for s in data.get("results", []):
                    scenarios.append({
                        "id": s.get("id"),
                        "name": s.get("name", ""),
                        "personality": s.get("personality", {}).get("name", "") if isinstance(s.get("personality"), dict) else "",
                    })
                if not data.get("next"):
                    break
                page += 1
            self.send_json({"ok": True, "scenarios": scenarios})
        except Exception as exc:  # noqa: BLE001
            self.send_json({"ok": False, "error": str(exc)}, status=502)

    def _serve_transcripts(self) -> None:
        """Scan harness/runs for live call transcripts and eval run results."""
        runs_dir = ROOT / "harness" / "runs"
        transcripts: list[dict[str, Any]] = []

        # 1. Live call transcripts: runs/transcripts/*.json
        live_dir = runs_dir / "transcripts"
        if live_dir.exists():
            for fpath in sorted(live_dir.glob("*.json")):
                try:
                    data = json.loads(fpath.read_text(encoding="utf-8"))
                    if data.get("source") != "live":
                        continue
                    ts_raw = data.get("timestamp", "")
                    # Generate a stable id from the filename
                    stem = fpath.stem  # e.g. 20260530T141500-live
                    entry_id = f"live-{stem.split('-')[0]}"
                    transcripts.append(
                        {
                            "id": entry_id,
                            "source": "live",
                            "title": "Live Call",
                            "timestamp": ts_raw,
                            "duration_s": data.get("duration_s"),
                            "passed": None,
                            "transcript": data.get("transcript", []),
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass

        # 2. Eval runs: runs/webhook-*/scenario-*.json
        for scenario_path in sorted(runs_dir.glob("webhook-*/scenario-*.json")):
            try:
                data = json.loads(scenario_path.read_text(encoding="utf-8"))
                # Derive timestamp from directory name: webhook-YYYYMMDDTHHMMSS
                dir_name = scenario_path.parent.name  # e.g. webhook-20260530T224023
                ts_match = re.search(r"(\d{8}T\d{6})", dir_name)
                ts_raw = ""
                if ts_match:
                    ts_str = ts_match.group(1)
                    try:
                        dt = datetime.strptime(ts_str, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
                        ts_raw = dt.isoformat()
                    except ValueError:
                        ts_raw = ts_str

                scenario_id = data.get("scenario_id", "")
                entry_id = f"eval-{scenario_id}-{dir_name}"

                # Try to get title from scenario name in the data
                title = str(data.get("scenario_name") or data.get("name") or f"Scenario {scenario_id}")

                # Look for transcript data
                transcript_data: list[dict[str, Any]] = []
                if "transcript" in data and isinstance(data["transcript"], list):
                    transcript_data = data["transcript"]
                else:
                    # Check for sibling transcript.json
                    sibling = scenario_path.parent / "transcript.json"
                    if sibling.exists():
                        try:
                            t = json.loads(sibling.read_text(encoding="utf-8"))
                            if isinstance(t, list):
                                transcript_data = t
                            elif isinstance(t, dict) and "transcript" in t:
                                transcript_data = t["transcript"]
                        except Exception:  # noqa: BLE001
                            pass

                passed_raw = data.get("passed")
                passed = bool(passed_raw) if passed_raw is not None else None

                transcripts.append(
                    {
                        "id": entry_id,
                        "source": "eval",
                        "title": title,
                        "timestamp": ts_raw,
                        "duration_s": None,
                        "passed": passed,
                        "transcript": transcript_data,
                    }
                )
            except Exception:  # noqa: BLE001
                pass

        # Sort by timestamp descending (most recent first)
        def sort_key(item: dict[str, Any]) -> str:
            return item.get("timestamp") or ""

        transcripts.sort(key=sort_key, reverse=True)
        self.send_json({"ok": True, "transcripts": transcripts})

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
        if parsed.path == "/api/run-scenario":
            self._handle_run_scenario()
            return
        if parsed.path == "/api/generate-scenario":
            self._handle_generate_scenario()
            return
        if parsed.path == "/api/create-scenario":
            self._handle_create_scenario()
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

        if errors and not enqueued:
            self.send_json({"ok": False, "error": errors[0], "errors": errors}, status=502)
            return
        self.send_json({"ok": True, "enqueued": enqueued, "errors": errors})

    def _read_json_body(self) -> dict[str, Any] | None:
        """Read and parse the JSON request body. Returns None and sends 400 on error."""
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length else b""
        try:
            return json.loads(body_bytes) if body_bytes else {}
        except Exception:
            self.send_json({"ok": False, "error": "Invalid JSON"}, status=400)
            return None

    def _handle_run_scenario(self) -> None:
        """Trigger a Cekura test run for one or more scenarios (no healing)."""
        body = self._read_json_body()
        if body is None:
            return
        scenario_names: list[str] = body.get("scenario_names", [])
        if not scenario_names:
            self.send_json({"ok": False, "error": "scenario_names is required"}, status=400)
            return

        try:
            id_map = _lookup_scenario_ids(scenario_names, self.server.state.cekura_agent_id)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"ok": False, "error": f"Cekura lookup failed: {exc}"}, status=502)
            return

        scenarios = [{"scenario": id_map[n]} for n in scenario_names if n in id_map]
        if not scenarios:
            self.send_json({"ok": False, "error": "No matching scenarios found in Cekura"}, status=404)
            return

        api_key = env_value("CEKURA_API_KEY")
        payload = json.dumps({"scenarios": scenarios, "frequency": 1}).encode()
        req = urllib.request.Request(
            f"{CEKURA_API_BASE}/test_framework/v1/scenarios/run_scenarios_pipecat_v2/",
            data=payload,
            headers={"Content-Type": "application/json", "X-CEKURA-API-KEY": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
            self.send_json({"ok": True, "run_id": result.get("id")})
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            self.send_json(
                {"ok": False, "error": f"Cekura error {exc.code}: {body_text[:300]}"}, status=502
            )
        except Exception as exc:  # noqa: BLE001
            self.send_json({"ok": False, "error": str(exc)}, status=500)

    def _handle_generate_scenario(self) -> None:
        body = self._read_json_body()
        if body is None:
            return
        description = str(body.get("description", "")).strip()
        if not description:
            self.send_json({"ok": False, "error": "description is required"}, status=400)
            return
        transcript_context = str(body.get("transcript_context", "")).strip()

        base_url = env_value("TOKEN_ROUTER_BASE_URL")
        api_key = env_value("TOKEN_ROUTER_API_KEY")
        if not base_url or not api_key:
            self.send_json(
                {"ok": False, "error": "TOKEN_ROUTER_BASE_URL / TOKEN_ROUTER_API_KEY not configured"},
                status=500,
            )
            return

        try:
            from openai import OpenAI  # noqa: PLC0415

            prompt_parts = [
                "Given this description of a voice agent test scenario, generate: "
                "a scenario name (short, descriptive), a caller persona (2-3 sentences "
                "describing who the caller is and what they do), and pass criteria "
                "(what the agent must do to pass).",
                f"Description: {description}",
            ]
            if transcript_context:
                prompt_parts.append(f"Transcript context:\n{transcript_context}")
            prompt_parts.append(
                'Respond with JSON only — no markdown fences: {"name": ..., "persona": ..., "pass_criteria": ...}'
            )
            prompt = "\n\n".join(prompt_parts)

            client = OpenAI(base_url=base_url, api_key=api_key)
            response = client.chat.completions.create(
                model="openai/gpt-5.5",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = (response.choices[0].message.content or "").strip()
            raw_text = re.sub(r"^```[a-z]*\n?", "", raw_text)
            raw_text = re.sub(r"\n?```$", "", raw_text)
            result = json.loads(raw_text)
            self.send_json(
                {
                    "ok": True,
                    "name": str(result.get("name", "")),
                    "persona": str(result.get("persona", "")),
                    "pass_criteria": str(result.get("pass_criteria", "")),
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.send_json({"ok": False, "error": str(exc)}, status=500)

    def _handle_create_scenario(self) -> None:
        body = self._read_json_body()
        if body is None:
            return
        name = str(body.get("name", "")).strip()
        persona = str(body.get("persona", "")).strip()
        pass_criteria = str(body.get("pass_criteria", "")).strip()
        if not name or not persona or not pass_criteria:
            self.send_json(
                {"ok": False, "error": "name, persona, and pass_criteria are required"},
                status=400,
            )
            return

        cekura_api_key = env_value("CEKURA_API_KEY")
        if not cekura_api_key:
            self.send_json({"ok": False, "error": "CEKURA_API_KEY not configured"}, status=500)
            return

        agent_id = self.server.state.cekura_agent_id
        payload = json.dumps(
            {
                "name": name,
                "agent": agent_id,
                "description": persona,
                "success_criteria": pass_criteria,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{CEKURA_API_BASE}/test_framework/v1/scenarios/",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-CEKURA-API-KEY": cekura_api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            self.send_json({"ok": True, "scenario_id": result.get("id")})
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            self.send_json(
                {"ok": False, "error": f"Cekura API error {exc.code}: {body_text[:300]}"},
                status=502,
            )
        except Exception as exc:  # noqa: BLE001
            self.send_json({"ok": False, "error": str(exc)}, status=500)

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
    parser = argparse.ArgumentParser(description="Serve the Voice Agent dashboard with manual Cekura refresh.")
    parser.add_argument("--host", default="127.0.0.1", help="Host for the local dashboard server.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port for the local dashboard server.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Dashboard run directory to serve.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Fallback input for first-time index generation.")
    parser.add_argument("--title", default="Voice Agent Self-Improvement Harness")
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
