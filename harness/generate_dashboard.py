#!/usr/bin/env python3
"""Generate a Bayview Pharmacy self-improvement dashboard.

The harness is intentionally dependency-free. It accepts Cekura-style JSON, a
plain transcript export, or the bundled sample, normalizes call evidence into a
small schema, classifies failures, and writes:

    - report.json: normalized machine-readable data
    - fix_plan.md: human-readable proposed remediation queue
    - index.html: static dashboard that can be opened directly in a browser
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "harness" / "examples" / "bayview_cekura_report_sample.json"
DEFAULT_OUT = ROOT / "harness" / "runs" / "latest"

KNOWN_DRUGS = [
    "lisinopril",
    "atorvastatin",
    "metformin",
    "levothyroxine",
    "albuterol",
    "amlodipine",
    "sertraline",
]

STATUS_PASS = {"pass", "passed", "success", "succeeded", "reviewed_success", "ok"}
STATUS_FAIL = {"fail", "failed", "failure", "reviewed_failure", "error"}
MONTH_WORDS = (
    "january|february|march|april|may|june|july|august|september|october|"
    "november|december"
)
DOB_RE = re.compile(
    rf"\b(\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}}/\d{{2,4}}|{MONTH_WORDS}|"
    r"nineteen|twenty|birthday|date of birth|dob)\b",
    re.IGNORECASE,
)
CALLER_CONFUSION_RE = re.compile(
    r"\b(are you still there|hello\??|can you hear me|confused|why do i|repeat|again)\b",
    re.IGNORECASE,
)
INFRA_RE = re.compile(
    r"\b(31921|31920|websocket|stream closed|hangup|hang up|twilio|capacity|timeout|"
    r"cold start|connection closed)\b",
    re.IGNORECASE,
)


@dataclass
class Turn:
    role: str
    text: str = ""
    name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    index: int = 0


@dataclass
class Metric:
    name: str
    status: str
    reason: str = ""
    score: float | None = None


@dataclass
class Facts:
    caller_intent: str = "unknown"
    identity_provided: bool = False
    identity_verified: bool = False
    verification_attempts: int = 0
    verification_failures: int = 0
    prescription_info_before_verification: bool = False
    get_prescriptions_before_verification: bool = False
    refill_before_verification: bool = False
    medication_requested: str = "unknown"
    refill_completed: bool = False
    end_call_called: bool = False
    early_end_call: bool = False
    caller_confusion: bool = False
    infra_signal: bool = False
    tool_errors: list[str] = field(default_factory=list)
    tool_sequence: list[str] = field(default_factory=list)


@dataclass
class RunRecord:
    run_id: str
    scenario_name: str
    status: str
    expected_outcome: list[str]
    metrics: list[Metric]
    turns: list[Turn]
    facts: Facts
    raw: dict[str, Any]


@dataclass
class Failure:
    id: str
    run_id: str
    scenario_name: str
    category: str
    severity: str
    title: str
    root_cause: str
    recommendation: str
    change_type: str
    confidence: str
    evidence: list[str]


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "item"


def normalize_status(value: Any) -> str:
    if value is None:
        return "unknown"
    text = str(value).strip().lower().replace(" ", "_")
    if text in STATUS_PASS:
        return "success"
    if text in STATUS_FAIL:
        return "failure"
    return text or "unknown"


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def text_from(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def get_any(mapping: dict[str, Any], names: list[str], default: Any = None) -> Any:
    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
    return default


def maybe_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    if text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def load_input(path: Path) -> Any:
    content = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return parse_plain_text_report(content)


def parse_plain_text_report(content: str) -> dict[str, Any]:
    """Best-effort parser for pasted transcript blocks.

    This is deliberately conservative. JSON exports produce better data, but
    text parsing makes the harness usable when a Cekura result is copied out of
    a chat or dashboard.
    """
    runs: list[dict[str, Any]] = []
    chunks = re.split(r"\n(?=#{1,4}\s+|Run ID:|Scenario:)", content)
    for idx, chunk in enumerate(chunks, start=1):
        if not chunk.strip():
            continue
        scenario_match = re.search(r"(?:Scenario|Test)\s*:\s*(.+)", chunk, re.IGNORECASE)
        status_match = re.search(r"(?:Status|Verdict|Result)\s*:\s*(\w+)", chunk, re.IGNORECASE)
        run_match = re.search(r"Run ID\s*:\s*([A-Za-z0-9_.:-]+)", chunk, re.IGNORECASE)
        if not (scenario_match or "assistant:" in chunk.lower() or "agent:" in chunk.lower()):
            continue
        runs.append(
            {
                "id": run_match.group(1) if run_match else f"text-run-{idx:03d}",
                "scenario_name": scenario_match.group(1).strip()
                if scenario_match
                else f"Transcript block {idx}",
                "evaluation_status": normalize_status(status_match.group(1) if status_match else None),
                "transcript": chunk,
            }
        )
    return {"source_format": "plain_text", "runs": runs}


def find_run_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (
        "runs",
        "workflow_runs",
        "test_runs",
        "call_logs",
        "calls",
        "evaluations",
        "results",
    ):
        value = payload.get(key)
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            return value
    if any(key in payload for key in ("transcript", "messages", "conversation", "scenario_name")):
        return [payload]
    return []


def normalize_expected(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [text_from(item).strip() for item in value if text_from(item).strip()]
    text = text_from(value)
    lines = []
    for line in text.splitlines():
        cleaned = re.sub(r"^\s*[-*0-9.)]+\s*", "", line).strip()
        if cleaned:
            lines.append(cleaned)
    return lines or ([text.strip()] if text.strip() else [])


def normalize_metrics(run: dict[str, Any]) -> list[Metric]:
    raw_metrics = get_any(run, ["metrics", "evaluations", "scores", "checks"], [])
    metrics: list[Metric] = []
    if isinstance(raw_metrics, dict):
        raw_metrics = [
            {"name": key, **value} if isinstance(value, dict) else {"name": key, "score": value}
            for key, value in raw_metrics.items()
        ]
    for idx, item in enumerate(as_list(raw_metrics), start=1):
        if not isinstance(item, dict):
            metrics.append(Metric(name=f"metric_{idx}", status=normalize_status(item), reason=text_from(item)))
            continue
        name = text_from(get_any(item, ["name", "metric", "title", "label"], f"metric_{idx}"))
        status = normalize_status(get_any(item, ["status", "verdict", "result", "evaluation_status"]))
        score_value = get_any(item, ["score", "value", "rating"])
        score = None
        if isinstance(score_value, (int, float)):
            score = float(score_value)
            if status == "unknown":
                status = "success" if score >= 0.8 else "failure"
        reason = text_from(get_any(item, ["reason", "explanation", "message", "details"], ""))
        metrics.append(Metric(name=name, status=status, reason=reason, score=score))
    return metrics


def parse_turns_from_text(transcript: str) -> list[Turn]:
    turns: list[Turn] = []
    current_role = ""
    current_text: list[str] = []

    def flush() -> None:
        nonlocal current_role, current_text
        if current_role or current_text:
            turns.append(
                Turn(
                    role=canonical_role(current_role or "unknown"),
                    text=" ".join(part.strip() for part in current_text if part.strip()),
                    index=len(turns),
                )
            )
        current_role = ""
        current_text = []

    line_re = re.compile(r"^\s*(user|caller|human|assistant|agent|bot|tool|function)\s*:\s*(.*)$", re.I)
    for line in transcript.splitlines():
        match = line_re.match(line)
        if match:
            flush()
            current_role = match.group(1)
            current_text = [match.group(2)]
            continue
        if line.strip():
            current_text.append(line)
    flush()
    return turns


def canonical_role(role: Any) -> str:
    text = text_from(role).strip().lower()
    if text in {"caller", "human", "customer", "patient"}:
        return "user"
    if text in {"agent", "bot", "ai"}:
        return "assistant"
    if text in {"function"}:
        return "tool"
    return text or "unknown"


def normalize_turn_item(item: dict[str, Any], index: int) -> list[Turn]:
    role = canonical_role(get_any(item, ["role", "speaker", "type", "author"], "unknown"))
    text = text_from(get_any(item, ["content", "text", "message", "utterance", "transcript"], ""))
    turns = [Turn(role=role, text=text, index=index)]

    if role == "tool" or any(key in item for key in ("tool_name", "function_name", "name")):
        turns[0].name = text_from(get_any(item, ["tool_name", "function_name", "name"], ""))
        turns[0].args = maybe_json(get_any(item, ["arguments", "args", "input"], {}))
        result = maybe_json(get_any(item, ["result", "output", "response"], None))
        if result is None and text and text.strip().startswith(("{", "[")):
            result = maybe_json(text)
        turns[0].result = result

    for tool_call in as_list(item.get("tool_calls")):
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else tool_call
        turns.append(
            Turn(
                role="tool",
                name=text_from(get_any(function, ["name", "tool_name", "function_name"], "")),
                args=maybe_json(get_any(function, ["arguments", "args", "input"], {})),
                result=maybe_json(get_any(tool_call, ["result", "output", "response"], None)),
                index=index,
            )
        )
    return turns


def normalize_turns(run: dict[str, Any]) -> list[Turn]:
    raw = get_any(run, ["transcript", "messages", "conversation", "turns", "events"], [])
    turns: list[Turn] = []
    if isinstance(raw, str):
        turns = parse_turns_from_text(raw)
    elif isinstance(raw, list):
        for idx, item in enumerate(raw):
            if isinstance(item, str):
                parsed = parse_turns_from_text(item)
                if parsed:
                    turns.extend(parsed)
                else:
                    turns.append(Turn(role="unknown", text=item, index=len(turns)))
            elif isinstance(item, dict):
                turns.extend(normalize_turn_item(item, len(turns)))
    elif isinstance(raw, dict):
        turns.extend(normalize_turn_item(raw, 0))

    for idx, tool_call in enumerate(as_list(get_any(run, ["tool_calls", "function_calls"], []))):
        if isinstance(tool_call, dict):
            turns.extend(normalize_turn_item({"role": "tool", **tool_call}, len(turns) + idx))

    for idx, turn in enumerate(turns):
        turn.index = idx
        if not isinstance(turn.args, dict):
            turn.args = {"value": turn.args}
    return turns


def tool_result_flag(result: Any, key: str) -> bool | None:
    if isinstance(result, dict) and key in result:
        return bool(result[key])
    return None


def result_has_error(result: Any) -> bool:
    if isinstance(result, dict):
        if result.get("error") or result.get("reason"):
            return True
        if result.get("ok") is False:
            return True
        if result.get("verified") is False:
            return False
    text = text_from(result).lower()
    return any(token in text for token in ("error", "failed", "not_found", "no prescription"))


def extract_facts(run: dict[str, Any], turns: list[Turn], status: str) -> Facts:
    facts = Facts()
    user_text = " ".join(turn.text for turn in turns if turn.role == "user").lower()
    assistant_text = " ".join(turn.text for turn in turns if turn.role == "assistant").lower()
    all_raw_text = json.dumps(run, ensure_ascii=True).lower()

    facts.caller_intent = infer_intent(user_text)
    facts.medication_requested = infer_medication(user_text + " " + assistant_text)
    facts.identity_provided = bool(DOB_RE.search(user_text))
    facts.caller_confusion = bool(CALLER_CONFUSION_RE.search(user_text))
    facts.infra_signal = bool(INFRA_RE.search(all_raw_text))

    verified = False
    for turn in turns:
        if turn.role != "tool":
            if not verified and turn.role == "assistant":
                lower_text = turn.text.lower()
                if any(drug in lower_text for drug in KNOWN_DRUGS):
                    facts.prescription_info_before_verification = True
            continue

        name = turn.name.lower()
        if name:
            facts.tool_sequence.append(name)

        if "verify_identity" in name:
            facts.verification_attempts += 1
            flag = tool_result_flag(turn.result, "verified")
            if flag:
                verified = True
                facts.identity_verified = True
            elif flag is False:
                facts.verification_failures += 1
        elif "get_prescriptions" in name:
            if not verified:
                facts.get_prescriptions_before_verification = True
            if result_has_error(turn.result):
                facts.tool_errors.append("get_prescriptions returned an error")
        elif "refill_prescription" in name:
            if not verified:
                facts.refill_before_verification = True
            if tool_result_flag(turn.result, "ok"):
                facts.refill_completed = True
            elif result_has_error(turn.result):
                facts.tool_errors.append("refill_prescription returned an error")
        elif "end_call" in name:
            facts.end_call_called = True

    facts.identity_provided = facts.identity_provided or facts.verification_attempts > 0
    expected_text = " ".join(normalize_expected(get_any(run, ["expected_outcome", "expected", "objective"]))).lower()
    expects_refill = "refill" in expected_text or facts.caller_intent == "refill_prescription"
    valid_failed_verification_end = facts.verification_failures >= 2 and not facts.identity_verified
    facts.early_end_call = (
        status == "failure"
        and facts.end_call_called
        and expects_refill
        and not facts.refill_completed
        and not valid_failed_verification_end
    )
    if (
        status == "failure"
        and len([turn for turn in turns if turn.role in {"user", "assistant"}]) <= 4
        and (facts.end_call_called or facts.infra_signal)
    ):
        facts.early_end_call = True
    return facts


def infer_intent(user_text: str) -> str:
    if re.search(r"\b(refill|fill|renew|prescription refill)\b", user_text):
        return "refill_prescription"
    if re.search(r"\b(ready|pickup|pick up|available)\b", user_text):
        return "pickup_status"
    if re.search(r"\b(medication|medicine|prescription|refills? left|what do i have)\b", user_text):
        return "medication_lookup"
    if re.search(r"\b(verify|identity|date of birth|dob)\b", user_text):
        return "identity_verification"
    return "unknown"


def infer_medication(text: str) -> str:
    for drug in KNOWN_DRUGS:
        if drug in text.lower():
            return drug
    return "unknown"


def normalize_run(raw: dict[str, Any], idx: int) -> RunRecord:
    run_id = text_from(get_any(raw, ["id", "run_id", "workflow_run_id", "call_id", "sid"], f"run-{idx:03d}"))
    scenario = text_from(
        get_any(raw, ["scenario_name", "scenario", "name", "title", "test_name"], f"Scenario {idx}")
    )
    status = normalize_status(get_any(raw, ["evaluation_status", "status", "verdict", "result", "outcome"]))
    metrics = normalize_metrics(raw)
    if status == "unknown" and metrics:
        status = "failure" if any(metric.status == "failure" for metric in metrics) else "success"
    turns = normalize_turns(raw)
    expected = normalize_expected(get_any(raw, ["expected_outcome", "expected", "objective", "criteria"], []))
    facts = extract_facts(raw, turns, status)
    return RunRecord(
        run_id=run_id,
        scenario_name=scenario,
        status=status,
        expected_outcome=expected,
        metrics=metrics,
        turns=turns,
        facts=facts,
        raw=raw,
    )


def evidence_from_turns(turns: list[Turn], patterns: list[re.Pattern[str]], limit: int = 3) -> list[str]:
    evidence: list[str] = []
    for turn in turns:
        line = f"{turn.role}: {turn.name or turn.text}".strip()
        haystack = f"{turn.name} {turn.text} {text_from(turn.result)}"
        if any(pattern.search(haystack) for pattern in patterns):
            evidence.append(line[:280])
        if len(evidence) >= limit:
            break
    return evidence


def metric_failure_evidence(metrics: list[Metric], limit: int = 3) -> list[str]:
    items = []
    for metric in metrics:
        if metric.status == "failure":
            suffix = f": {metric.reason}" if metric.reason else ""
            items.append(f"{metric.name}{suffix}"[:280])
        if len(items) >= limit:
            break
    return items


def classify_failures(record: RunRecord) -> list[Failure]:
    failures: list[Failure] = []
    facts = record.facts
    failed_metric_evidence = metric_failure_evidence(record.metrics)
    base = {
        "run_id": record.run_id,
        "scenario_name": record.scenario_name,
    }

    def add(
        category: str,
        severity: str,
        title: str,
        root_cause: str,
        recommendation: str,
        change_type: str,
        confidence: str,
        evidence: list[str],
    ) -> None:
        if not evidence:
            evidence.extend(failed_metric_evidence[:2])
        if not evidence:
            evidence.append(f"Status: {record.status}")
        failures.append(
            Failure(
                id=f"{record.run_id}-{slugify(category)}-{len(failures) + 1}",
                category=category,
                severity=severity,
                title=title,
                root_cause=root_cause,
                recommendation=recommendation,
                change_type=change_type,
                confidence=confidence,
                evidence=evidence[:4],
                **base,
            )
        )

    if (
        facts.prescription_info_before_verification
        or facts.get_prescriptions_before_verification
        or facts.refill_before_verification
    ):
        add(
            "Privacy Risk",
            "critical",
            "Prescription data was exposed or changed before identity verification",
            "The prompt says verification is mandatory, but the tool layer can still be reached before verified state is enforced.",
            "Add a code guard to prescription lookup/refill tools and keep the prompt rule as a secondary control.",
            "code guardrail",
            "high",
            evidence_from_turns(
                record.turns,
                [
                    re.compile(r"get_prescriptions|refill_prescription", re.I),
                    re.compile("|".join(KNOWN_DRUGS), re.I),
                ],
            ),
        )

    if facts.infra_signal:
        add(
            "Infra Issue",
            "high",
            "Call path has Twilio/Pipecat transport failure signals",
            "The transcript or run metadata contains websocket, Twilio, capacity, or hangup indicators.",
            "Inspect Twilio notifications and Pipecat session capacity before changing the prompt.",
            "deployment/config",
            "medium",
            [text_from(get_any(record.raw, ["error", "error_code", "message", "reason"], ""))]
            + failed_metric_evidence,
        )

    if facts.early_end_call:
        add(
            "Early End Call",
            "high",
            "Agent ended before completing the expected pharmacy task",
            "The call reached end_call while the expected refill or lookup flow was incomplete.",
            "Gate end_call on task completion state and tighten the closing rule in the system prompt.",
            "prompt + orchestration",
            "high",
            evidence_from_turns(record.turns, [re.compile(r"end_call|goodbye|take care", re.I)]),
        )

    if (
        record.status == "failure"
        and facts.identity_provided
        and facts.verification_attempts == 0
        and facts.caller_intent in {"refill_prescription", "pickup_status", "medication_lookup"}
    ):
        add(
            "Identity Flow Gap",
            "high",
            "Caller provided identity details but verify_identity was not called",
            "The agent collected enough identity information but did not advance to the verification tool.",
            "Add a prompt rule and state check: after full name plus DOB are collected, call verify_identity immediately.",
            "prompt",
            "high",
            evidence_from_turns(record.turns, [DOB_RE]),
        )

    if facts.caller_confusion and record.status == "failure":
        add(
            "Conversation Responsiveness",
            "medium",
            "Caller had to check whether the agent was still present",
            "The agent likely left long pauses or gave unclear acknowledgement during a required step.",
            "Add a concise keepalive/acknowledgement policy for long tool or model waits.",
            "prompt + latency handling",
            "medium",
            evidence_from_turns(record.turns, [CALLER_CONFUSION_RE]),
        )

    if facts.tool_errors and record.status == "failure":
        add(
            "Tool/Backend Issue",
            "medium",
            "A tool returned an error during the workflow",
            "The call failed after a backend/tool result did not support the scenario's expected path.",
            "Decide whether the mock backend data or the scenario expectation is wrong, then update the failing side.",
            "mock data or evaluator",
            "medium",
            facts.tool_errors,
        )

    if record.status == "failure" and failed_metric_evidence and not failures:
        add(
            "Metric or Prompt Ambiguity",
            "medium",
            "Run failed without a deterministic behavioral signal",
            "The metric explanation needs review, or the prompt lacks enough specificity for this scenario.",
            "Inspect the metric explanation, then either tune the metric or add a narrow prompt clarification.",
            "metric or prompt",
            "medium",
            failed_metric_evidence,
        )

    if record.status == "failure" and not failures:
        add(
            "Unclassified Failure",
            "medium",
            "Run failed but the harness could not classify the failure",
            "The export did not include enough structured evidence for an automated diagnosis.",
            "Open the transcript in the Evidence Explorer and add a custom classifier if this repeats.",
            "manual review",
            "low",
            [],
        )

    return failures


def severity_rank(severity: str) -> int:
    return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(severity, 4)


def build_clusters(failures: list[Failure]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Failure]] = {}
    for failure in failures:
        key = f"{failure.category}|{failure.change_type}"
        grouped.setdefault(key, []).append(failure)

    clusters = []
    for idx, (key, items) in enumerate(grouped.items(), start=1):
        category, change_type = key.split("|", 1)
        items = sorted(items, key=lambda item: (severity_rank(item.severity), item.scenario_name))
        clusters.append(
            {
                "id": f"cluster-{idx:02d}-{slugify(category)}",
                "category": category,
                "change_type": change_type,
                "severity": items[0].severity,
                "title": items[0].title,
                "run_count": len({item.run_id for item in items}),
                "scenarios": sorted({item.scenario_name for item in items}),
                "root_cause": items[0].root_cause,
                "recommendation": items[0].recommendation,
                "confidence": items[0].confidence,
                "failures": [asdict(item) for item in items],
            }
        )
    return sorted(clusters, key=lambda cluster: (severity_rank(cluster["severity"]), -cluster["run_count"]))


def build_fix_queue(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queue = []
    file_targets = {
        "code guardrail": "server/bot-nemotron.py, server/bot-gpt.py",
        "prompt": "server/bot-nemotron.py, server/bot-gpt.py",
        "prompt + orchestration": "server/bot-nemotron.py, server/bot-gpt.py",
        "deployment/config": "server/pcc-deploy.toml, Twilio/Pipecat settings",
        "metric or prompt": "Cekura metric or system prompt",
        "mock data or evaluator": "server/mock_backend.py or Cekura scenario",
    }
    for idx, cluster in enumerate(clusters, start=1):
        queue.append(
            {
                "priority": idx,
                "id": cluster["id"].replace("cluster", "fix", 1),
                "severity": cluster["severity"],
                "category": cluster["category"],
                "change_type": cluster["change_type"],
                "affected_runs": cluster["run_count"],
                "title": cluster["title"],
                "action": cluster["recommendation"],
                "confidence": cluster["confidence"],
                "target": file_targets.get(cluster["change_type"], "manual review"),
                "regression_risk": infer_regression_risk(cluster),
            }
        )
    return queue


def infer_regression_risk(cluster: dict[str, Any]) -> str:
    if cluster["category"] == "Privacy Risk":
        return "Medium: add focused guardrail tests for all verified happy paths."
    if cluster["category"] == "Early End Call":
        return "Medium: verify normal goodbye still hangs up cleanly."
    if cluster["category"] == "Infra Issue":
        return "Low: config-only, but watch cost and concurrency."
    if "Metric" in cluster["category"]:
        return "Low for agent behavior, medium for score comparability."
    return "Medium: re-run the full Bayview scenario suite."


def build_matrix(records: list[RunRecord]) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        rows.append(
            {
                "run_id": record.run_id,
                "scenario_name": record.scenario_name,
                "status": record.status,
                "intent": record.facts.caller_intent,
                "identity_verified": record.facts.identity_verified,
                "refill_completed": record.facts.refill_completed,
                "privacy_risk": record.facts.prescription_info_before_verification
                or record.facts.get_prescriptions_before_verification
                or record.facts.refill_before_verification,
            }
        )
    return rows


def build_summary(records: list[RunRecord], failures: list[Failure]) -> dict[str, Any]:
    total = len(records)
    passed = len([record for record in records if record.status == "success"])
    failed = len([record for record in records if record.status == "failure"])
    critical = len([failure for failure in failures if failure.severity == "critical"])
    pass_rate = round((passed / total) * 100, 1) if total else 0.0
    return {
        "total_runs": total,
        "passed_runs": passed,
        "failed_runs": failed,
        "pass_rate": pass_rate,
        "failure_count": len(failures),
        "critical_count": critical,
        "open_fix_count": len({f"{failure.category}|{failure.change_type}" for failure in failures}),
    }


def build_model(payload: Any, source_path: Path, title: str) -> dict[str, Any]:
    raw_runs = find_run_items(payload)
    records = [normalize_run(item, idx) for idx, item in enumerate(raw_runs, start=1)]
    failures = [failure for record in records for failure in classify_failures(record)]
    clusters = build_clusters(failures)
    agent_payload = payload.get("agent", {}) if isinstance(payload, dict) else {}
    agent_name = text_from(get_any(agent_payload, ["name", "agent_name"], "Bayview Pharmacy"))
    result_id = text_from(
        get_any(payload, ["result_id", "id", "report_id", "benchmark_id"], source_path.stem)
        if isinstance(payload, dict)
        else source_path.stem
    )
    return {
        "title": title,
        "generated_at": now_iso(),
        "source": str(source_path),
        "agent": {
            "name": agent_name,
            "result_id": result_id,
            "deployment_id": text_from(
                get_any(agent_payload, ["deployment_id", "pipecat_deployment_id"], "")
            ),
            "model_stack": text_from(get_any(agent_payload, ["model_stack", "stack"], "")),
        },
        "summary": build_summary(records, failures),
        "runs": [serialize_run(record) for record in records],
        "failures": [asdict(failure) for failure in failures],
        "clusters": clusters,
        "fix_queue": build_fix_queue(clusters),
        "scenario_matrix": build_matrix(records),
    }


def serialize_run(record: RunRecord) -> dict[str, Any]:
    data = asdict(record)
    data.pop("raw", None)
    return data


def write_report(model: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(model, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "fix_plan.md").write_text(render_fix_plan(model), encoding="utf-8")
    (out_dir / "index.html").write_text(render_html(model), encoding="utf-8")


def render_fix_plan(model: dict[str, Any]) -> str:
    lines = [
        f"# {model['title']} Fix Plan",
        "",
        f"Generated: {model['generated_at']}",
        f"Source: `{model['source']}`",
        "",
        "## Summary",
        "",
        f"- Pass rate: {model['summary']['pass_rate']}%",
        f"- Runs: {model['summary']['total_runs']}",
        f"- Failed runs: {model['summary']['failed_runs']}",
        f"- Failure findings: {model['summary']['failure_count']}",
        f"- Open fix clusters: {model['summary']['open_fix_count']}",
        "",
        "## Fix Queue",
        "",
    ]
    if not model["fix_queue"]:
        lines.append("No open fixes. Run the full regression suite before declaring success.")
        lines.append("")
        return "\n".join(lines)

    cluster_by_id = {cluster["id"].replace("cluster", "fix", 1): cluster for cluster in model["clusters"]}
    for item in model["fix_queue"]:
        cluster = cluster_by_id.get(item["id"], {})
        lines.extend(
            [
                f"### P{item['priority']} - {item['title']}",
                "",
                f"- Severity: `{item['severity']}`",
                f"- Category: `{item['category']}`",
                f"- Change type: `{item['change_type']}`",
                f"- Target: `{item['target']}`",
                f"- Affected runs: {item['affected_runs']}",
                f"- Confidence: `{item['confidence']}`",
                f"- Regression risk: {item['regression_risk']}",
                "",
                f"Action: {item['action']}",
                "",
                "Evidence:",
            ]
        )
        for failure in cluster.get("failures", [])[:3]:
            evidence = "; ".join(failure.get("evidence", [])[:2])
            lines.append(f"- `{failure['run_id']}` {failure['scenario_name']}: {evidence}")
        lines.append("")
    return "\n".join(lines)


def render_html(model: dict[str, Any]) -> str:
    dashboard_json = json.dumps(model, ensure_ascii=True).replace("</", "<\\/")
    return (
        HTML_TEMPLATE.replace("__TITLE__", escape(model["title"]))
        .replace("__GENERATED_AT__", escape(model["generated_at"]))
        .replace("__DASHBOARD_JSON__", dashboard_json)
    )


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <style>
    :root {
      --bg: #f6f7f4;
      --panel: #ffffff;
      --ink: #17201b;
      --muted: #66736d;
      --line: #d8ddd7;
      --teal: #087f7a;
      --green: #1d7c43;
      --amber: #a56710;
      --red: #b42318;
      --blue: #2864a8;
      --shadow: 0 8px 24px rgba(23, 32, 27, 0.08);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      letter-spacing: 0;
    }
    button, input {
      font: inherit;
    }
    .app {
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr;
    }
    header {
      background: #ffffff;
      border-bottom: 1px solid var(--line);
      padding: 18px 22px;
      position: sticky;
      top: 0;
      z-index: 4;
    }
    .header-row {
      max-width: 1440px;
      margin: 0 auto;
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
    }
    h1 {
      margin: 0;
      font-size: 22px;
      line-height: 1.2;
      font-weight: 720;
    }
    .subhead {
      margin-top: 5px;
      color: var(--muted);
      font-size: 13px;
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    .toolbar {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .segmented {
      display: inline-flex;
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background: #fafbf8;
    }
    .segmented button {
      border: 0;
      border-right: 1px solid var(--line);
      background: transparent;
      padding: 8px 10px;
      cursor: pointer;
      color: var(--muted);
    }
    .segmented button:last-child { border-right: 0; }
    .segmented button.active {
      background: var(--teal);
      color: #fff;
    }
    main {
      max-width: 1440px;
      width: 100%;
      margin: 0 auto;
      padding: 18px 22px 28px;
      display: grid;
      gap: 16px;
    }
    .kpis {
      display: grid;
      grid-template-columns: repeat(5, minmax(150px, 1fr));
      gap: 12px;
    }
    .kpi, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    .kpi {
      padding: 14px;
      min-height: 92px;
      display: grid;
      align-content: space-between;
    }
    .kpi-label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0;
    }
    .kpi-value {
      font-size: 26px;
      line-height: 1.1;
      font-weight: 760;
      margin-top: 8px;
    }
    .kpi small {
      color: var(--muted);
      font-size: 12px;
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(280px, 0.85fr) minmax(460px, 1.4fr) minmax(320px, 0.95fr);
      gap: 16px;
      align-items: start;
    }
    .panel {
      min-width: 0;
      overflow: hidden;
    }
    .panel-title {
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .panel-title h2 {
      margin: 0;
      font-size: 14px;
      line-height: 1.25;
    }
    .panel-body {
      padding: 14px 16px;
    }
    .cluster-list {
      display: grid;
      gap: 8px;
      max-height: 640px;
      overflow: auto;
      padding: 10px;
    }
    .cluster-button {
      width: 100%;
      text-align: left;
      background: #fbfcfa;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 11px;
      cursor: pointer;
      display: grid;
      gap: 8px;
    }
    .cluster-button.active {
      border-color: var(--teal);
      box-shadow: inset 3px 0 0 var(--teal);
      background: #f1fbf8;
    }
    .cluster-top {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 10px;
    }
    .cluster-name {
      font-weight: 680;
      font-size: 13px;
      line-height: 1.25;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 11px;
      font-weight: 650;
      white-space: nowrap;
      border: 1px solid transparent;
    }
    .critical { color: #fff; background: var(--red); }
    .high { color: #4c2a04; background: #ffdc8a; border-color: #e9bd59; }
    .medium { color: #17395e; background: #d9ebff; border-color: #b9d4f0; }
    .low { color: #24503a; background: #d9f2df; border-color: #bce0c5; }
    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
      color: var(--muted);
      font-size: 12px;
    }
    .detail h3 {
      margin: 0 0 8px;
      font-size: 20px;
      line-height: 1.25;
    }
    .detail p {
      color: var(--muted);
      line-height: 1.5;
      margin: 8px 0;
      font-size: 14px;
    }
    .evidence {
      display: grid;
      gap: 9px;
      margin-top: 12px;
    }
    .evidence-item {
      border-left: 3px solid var(--teal);
      background: #f8faf7;
      padding: 9px 10px;
      font-size: 13px;
      line-height: 1.45;
      color: #26322d;
      overflow-wrap: anywhere;
    }
    .fix-list {
      display: grid;
      gap: 10px;
      max-height: 640px;
      overflow: auto;
    }
    .fix-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfa;
    }
    .fix-item h3 {
      margin: 0;
      font-size: 14px;
      line-height: 1.25;
    }
    .fix-item p {
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }
    .matrix {
      overflow-x: auto;
    }
    table {
      border-collapse: collapse;
      width: 100%;
      min-width: 760px;
      font-size: 13px;
    }
    th, td {
      padding: 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0;
      background: #fbfcfa;
    }
    .status-dot {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-weight: 650;
      text-transform: capitalize;
    }
    .status-dot::before {
      content: "";
      width: 9px;
      height: 9px;
      border-radius: 999px;
      background: var(--muted);
      flex: 0 0 auto;
    }
    .status-success::before { background: var(--green); }
    .status-failure::before { background: var(--red); }
    .status-unknown::before { background: var(--amber); }
    .hidden { display: none !important; }
    @media (max-width: 1120px) {
      .grid { grid-template-columns: 1fr; }
      .kpis { grid-template-columns: repeat(2, minmax(150px, 1fr)); }
    }
    @media (max-width: 640px) {
      header, main { padding-left: 14px; padding-right: 14px; }
      .header-row { align-items: flex-start; flex-direction: column; }
      .toolbar { justify-content: flex-start; }
      .kpis { grid-template-columns: 1fr; }
      h1 { font-size: 19px; }
      .kpi-value { font-size: 23px; }
    }
  </style>
</head>
<body>
<div class="app">
  <header>
    <div class="header-row">
      <div>
        <h1 id="page-title">__TITLE__</h1>
        <div class="subhead">
          <span id="agent-name"></span>
          <span id="result-id"></span>
          <span>Generated __GENERATED_AT__</span>
        </div>
      </div>
      <div class="toolbar">
        <div class="segmented" aria-label="Dashboard section">
          <button class="active" data-view="overview">Overview</button>
          <button data-view="matrix">Matrix</button>
          <button data-view="runs">Runs</button>
        </div>
      </div>
    </div>
  </header>
  <main>
    <section class="kpis" id="kpis"></section>
    <section class="grid view view-overview">
      <div class="panel">
        <div class="panel-title"><h2>Failure Clusters</h2><span id="cluster-count" class="pill low"></span></div>
        <div class="cluster-list" id="cluster-list"></div>
      </div>
      <div class="panel">
        <div class="panel-title"><h2>Evidence Explorer</h2><span id="detail-severity"></span></div>
        <div class="panel-body detail" id="cluster-detail"></div>
      </div>
      <div class="panel">
        <div class="panel-title"><h2>Fix Queue</h2><span id="fix-count" class="pill medium"></span></div>
        <div class="panel-body fix-list" id="fix-list"></div>
      </div>
    </section>
    <section class="panel view view-matrix hidden">
      <div class="panel-title"><h2>Scenario Matrix</h2><span class="pill low">one run export</span></div>
      <div class="panel-body matrix" id="matrix"></div>
    </section>
    <section class="panel view view-runs hidden">
      <div class="panel-title"><h2>Run Facts</h2><span class="pill low">normalized evidence</span></div>
      <div class="panel-body matrix" id="runs"></div>
    </section>
  </main>
</div>
<script id="dashboard-data" type="application/json">__DASHBOARD_JSON__</script>
<script>
const model = JSON.parse(document.getElementById("dashboard-data").textContent);
let selectedClusterId = model.clusters[0]?.id || null;

const severityClass = (value) => ["critical", "high", "medium", "low"].includes(value) ? value : "low";
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[char]));

function renderKpis() {
  const s = model.summary;
  const items = [
    ["Pass Rate", `${s.pass_rate}%`, `${s.passed_runs}/${s.total_runs} runs passed`],
    ["Failed Runs", s.failed_runs, "Cekura failures to inspect"],
    ["Findings", s.failure_count, "Behavioral issues found"],
    ["Critical", s.critical_count, "Privacy or safety priority"],
    ["Open Fixes", s.open_fix_count, "Clustered change proposals"]
  ];
  document.getElementById("kpis").innerHTML = items.map(([label, value, note]) => `
    <div class="kpi">
      <div class="kpi-label">${escapeHtml(label)}</div>
      <div class="kpi-value">${escapeHtml(value)}</div>
      <small>${escapeHtml(note)}</small>
    </div>
  `).join("");
}

function renderClusters() {
  const list = document.getElementById("cluster-list");
  document.getElementById("cluster-count").textContent = `${model.clusters.length} clusters`;
  if (!model.clusters.length) {
    list.innerHTML = `<div class="panel-body">No failure clusters. Run regression before declaring success.</div>`;
    renderClusterDetail(null);
    return;
  }
  list.innerHTML = model.clusters.map((cluster) => `
    <button class="cluster-button ${cluster.id === selectedClusterId ? "active" : ""}" data-cluster="${escapeHtml(cluster.id)}">
      <div class="cluster-top">
        <div class="cluster-name">${escapeHtml(cluster.title)}</div>
        <span class="pill ${severityClass(cluster.severity)}">${escapeHtml(cluster.severity)}</span>
      </div>
      <div class="meta">
        <span>${escapeHtml(cluster.category)}</span>
        <span>${escapeHtml(cluster.run_count)} run(s)</span>
        <span>${escapeHtml(cluster.change_type)}</span>
      </div>
    </button>
  `).join("");
  list.querySelectorAll("[data-cluster]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedClusterId = button.dataset.cluster;
      renderClusters();
      renderClusterDetail(model.clusters.find((cluster) => cluster.id === selectedClusterId));
    });
  });
}

function renderClusterDetail(cluster) {
  const severity = document.getElementById("detail-severity");
  const detail = document.getElementById("cluster-detail");
  if (!cluster) {
    severity.innerHTML = "";
    detail.innerHTML = `<h3>No open failures</h3><p>The latest report did not produce fix clusters.</p>`;
    return;
  }
  severity.innerHTML = `<span class="pill ${severityClass(cluster.severity)}">${escapeHtml(cluster.severity)}</span>`;
  const evidence = cluster.failures.flatMap((failure) =>
    failure.evidence.map((item) => ({ run: failure.run_id, scenario: failure.scenario_name, item }))
  ).slice(0, 8);
  detail.innerHTML = `
    <h3>${escapeHtml(cluster.title)}</h3>
    <div class="meta">
      <span>${escapeHtml(cluster.category)}</span>
      <span>${escapeHtml(cluster.change_type)}</span>
      <span>${escapeHtml(cluster.confidence)} confidence</span>
    </div>
    <p><strong>Root cause:</strong> ${escapeHtml(cluster.root_cause)}</p>
    <p><strong>Recommended change:</strong> ${escapeHtml(cluster.recommendation)}</p>
    <p><strong>Affected scenarios:</strong> ${escapeHtml(cluster.scenarios.join(", "))}</p>
    <div class="evidence">
      ${evidence.map((entry) => `
        <div class="evidence-item">
          <strong>${escapeHtml(entry.run)}</strong> ${escapeHtml(entry.scenario)}<br>
          ${escapeHtml(entry.item)}
        </div>
      `).join("")}
    </div>
  `;
}

function renderFixQueue() {
  const list = document.getElementById("fix-list");
  document.getElementById("fix-count").textContent = `${model.fix_queue.length} fixes`;
  if (!model.fix_queue.length) {
    list.innerHTML = `<p>No fixes queued.</p>`;
    return;
  }
  list.innerHTML = model.fix_queue.map((item) => `
    <div class="fix-item">
      <div class="cluster-top">
        <h3>P${escapeHtml(item.priority)} ${escapeHtml(item.title)}</h3>
        <span class="pill ${severityClass(item.severity)}">${escapeHtml(item.severity)}</span>
      </div>
      <p><strong>Target:</strong> ${escapeHtml(item.target)}</p>
      <p>${escapeHtml(item.action)}</p>
      <p><strong>Regression risk:</strong> ${escapeHtml(item.regression_risk)}</p>
    </div>
  `).join("");
}

function statusMarkup(status) {
  const normalized = ["success", "failure"].includes(status) ? status : "unknown";
  return `<span class="status-dot status-${normalized}">${escapeHtml(status)}</span>`;
}

function renderMatrix() {
  document.getElementById("matrix").innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Scenario</th><th>Status</th><th>Intent</th><th>Identity Verified</th>
          <th>Refill Complete</th><th>Privacy Risk</th>
        </tr>
      </thead>
      <tbody>
        ${model.scenario_matrix.map((row) => `
          <tr>
            <td><strong>${escapeHtml(row.scenario_name)}</strong><br><span class="meta">${escapeHtml(row.run_id)}</span></td>
            <td>${statusMarkup(row.status)}</td>
            <td>${escapeHtml(row.intent)}</td>
            <td>${row.identity_verified ? "yes" : "no"}</td>
            <td>${row.refill_completed ? "yes" : "no"}</td>
            <td>${row.privacy_risk ? "yes" : "no"}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function renderRuns() {
  document.getElementById("runs").innerHTML = `
    <table>
      <thead>
        <tr><th>Run</th><th>Status</th><th>Facts</th><th>Tools</th><th>Expected</th></tr>
      </thead>
      <tbody>
        ${model.runs.map((run) => `
          <tr>
            <td><strong>${escapeHtml(run.scenario_name)}</strong><br><span class="meta">${escapeHtml(run.run_id)}</span></td>
            <td>${statusMarkup(run.status)}</td>
            <td>
              intent=${escapeHtml(run.facts.caller_intent)}<br>
              identity_verified=${run.facts.identity_verified ? "yes" : "no"}<br>
              early_end=${run.facts.early_end_call ? "yes" : "no"}
            </td>
            <td>${escapeHtml(run.facts.tool_sequence.join(", ") || "none")}</td>
            <td>${escapeHtml((run.expected_outcome || []).join(" | "))}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function setupViews() {
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-view]").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      document.querySelectorAll(".view").forEach((view) => view.classList.add("hidden"));
      document.querySelector(`.view-${button.dataset.view}`).classList.remove("hidden");
    });
  });
}

function init() {
  document.getElementById("agent-name").textContent = model.agent.name || "Bayview Pharmacy";
  document.getElementById("result-id").textContent = model.agent.result_id ? `Result ${model.agent.result_id}` : "";
  renderKpis();
  renderClusters();
  renderClusterDetail(model.clusters.find((cluster) => cluster.id === selectedClusterId));
  renderFixQueue();
  renderMatrix();
  renderRuns();
  setupViews();
}

init();
</script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the Bayview self-improvement dashboard.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Cekura-style JSON or text report.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output directory for dashboard files.")
    parser.add_argument("--title", default="Bayview Pharmacy Self-Improvement Harness")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = args.input.resolve()
    out_dir = args.out.resolve()
    payload = load_input(input_path)
    model = build_model(payload, input_path, args.title)
    write_report(model, out_dir)
    print(f"Wrote dashboard: {out_dir / 'index.html'}")
    print(f"Wrote normalized report: {out_dir / 'report.json'}")
    print(f"Wrote fix plan: {out_dir / 'fix_plan.md'}")


if __name__ == "__main__":
    main()
