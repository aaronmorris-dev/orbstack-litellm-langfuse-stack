#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "langfuse>=3.0",
#     "openai>=1.0",
# ]
# ///
"""
Evaluate a Langfuse session using LLM-as-judge and post scores back.

Usage:
  uv run --script scripts/eval-session.py <session-id>
  uv run --script scripts/eval-session.py --today opencode
  uv run --script scripts/eval-session.py <session-id> --dry-run --verbose
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


MAX_TRACES = 500
PAGE_SIZE = 100
MAX_TIMELINE_EVENTS = 100
DEFAULT_LANGFUSE_HOST = "http://localhost:5002"
LITELLM_BASE_URL = "http://localhost:4000"
LITELLM_MODEL = os.environ.get("EVAL_MODEL", "gemini-2.5-flash")
LITELLM_API_KEY = os.environ.get("LITELLM_KEY", "")
GATEWAY_ENV_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "gateway-env.sh"
)

# Score config IDs registered in Langfuse (created via POST /api/public/score-configs).
# Set these after creating score configs in your Langfuse instance, or leave empty.
SCORE_CONFIGS: dict[str, str] = {
    "task_completion": os.environ.get("SCORE_CONFIG_TASK_COMPLETION", ""),
    "approach_quality": os.environ.get("SCORE_CONFIG_APPROACH_QUALITY", ""),
    "communication": os.environ.get("SCORE_CONFIG_COMMUNICATION", ""),
    "overall": os.environ.get("SCORE_CONFIG_OVERALL", ""),
}


@dataclass
class TimelineEvent:
    timestamp: str
    name: str
    model: str
    user_input: str
    assistant_output: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate a Langfuse session and post session-level scores.",
    )
    parser.add_argument(
        "session_id",
        nargs="?",
        help="Session ID, e.g. opencode-2026-03-10",
    )
    parser.add_argument(
        "--today",
        metavar="TOOL",
        help="Build session id as <tool>-YYYY-MM-DD (e.g. --today opencode)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and reconstruct only; skip judge and score posting",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed timeline and diagnostic output",
    )
    args = parser.parse_args()

    if bool(args.session_id) == bool(args.today):
        parser.error("Provide exactly one of <session-id> or --today <tool>.")

    if args.today:
        today = dt.date.today().isoformat()
        args.session_id = f"{args.today}-{today}"

    return args


def debug(enabled: bool, message: str) -> None:
    if enabled:
        print(f"[debug] {message}")


def ensure_langfuse_env(verbose: bool) -> None:
    required = ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]
    if all(os.environ.get(key) for key in required):
        return

    debug(
        verbose,
        "LANGFUSE keys missing in environment; attempting to source gateway-env.sh",
    )
    cmd = (
        f"source '{GATEWAY_ENV_SCRIPT}' >/dev/null 2>&1 && "
        "python3 - <<'PY'\n"
        "import json, os\n"
        "keys = ['LANGFUSE_PUBLIC_KEY','LANGFUSE_SECRET_KEY','LANGFUSE_HOST','LITELLM_KEY']\n"
        "print(json.dumps({k: os.environ.get(k) for k in keys}))\n"
        "PY"
    )
    try:
        proc = subprocess.run(
            ["bash", "-lc", cmd],
            capture_output=True,
            text=True,
            check=True,
        )
        values = json.loads(proc.stdout.strip())
        for key, value in values.items():
            if value and not os.environ.get(key):
                os.environ[key] = value
    except Exception as exc:
        debug(verbose, f"Unable to source gateway-env.sh: {exc}")

    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "Missing required env vars after sourcing attempt: " + ", ".join(missing)
        )


def build_langfuse_client(verbose: bool) -> Any:
    ensure_langfuse_env(verbose)
    host = os.environ.get("LANGFUSE_HOST", DEFAULT_LANGFUSE_HOST)
    debug(verbose, f"Using Langfuse host: {host}")
    import importlib

    Langfuse = importlib.import_module("langfuse").Langfuse

    return Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        host=host,
    )


def _trace_list_page(langfuse: Any, session_id: str, page: int) -> list[Any]:
    response = langfuse.api.trace.list(
        session_id=session_id, limit=PAGE_SIZE, page=page
    )
    data = getattr(response, "data", response)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    return []


def fetch_traces(langfuse: Any, session_id: str, verbose: bool) -> list[Any]:
    traces: list[Any] = []
    page = 1
    while len(traces) < MAX_TRACES:
        batch = _trace_list_page(langfuse, session_id, page)
        if not batch:
            break
        traces.extend(batch)
        debug(verbose, f"Fetched page {page}: {len(batch)} traces")
        if len(batch) < PAGE_SIZE:
            break
        page += 1
    traces = traces[:MAX_TRACES]

    def ts_of(trace: Any) -> str:
        value = get_field(trace, "timestamp") or get_field(trace, "createdAt") or ""
        return str(value)

    traces.sort(key=ts_of)
    return traces


def get_field(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [normalize_text(item) for item in value]
        return " ".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"].strip()
        if isinstance(value.get("content"), str):
            return value["content"].strip()
        return json.dumps(value, ensure_ascii=True)
    return str(value).strip()


def extract_last_user_message(raw_input: Any) -> str:
    if isinstance(raw_input, list):
        for msg in reversed(raw_input):
            role = str(get_field(msg, "role", "")).lower()
            if role == "user":
                return normalize_text(get_field(msg, "content"))
    return normalize_text(raw_input)


def _looks_like_tool_call(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return False
    tool_markers = [
        '"name":',
        '"call_id":',
        '"function_call"',
        '"type": "function',
        '"apply_patch"',
        '"arguments":',
        '"tool_calls"',
    ]
    return any(marker in stripped[:500] for marker in tool_markers)


def extract_first_output_message(raw_output: Any) -> str:
    if isinstance(raw_output, list) and raw_output:
        text = normalize_text(get_field(raw_output[0], "content", raw_output[0]))
    elif isinstance(raw_output, dict):
        choices = get_field(raw_output, "choices")
        if isinstance(choices, list) and choices:
            message = get_field(choices[0], "message")
            text = normalize_text(get_field(message, "content"))
        else:
            text = normalize_text(raw_output)
    else:
        text = normalize_text(raw_output)
    return "" if _looks_like_tool_call(text) else text


def truncate(text: str, max_len: int = 200) -> str:
    clean = " ".join(text.split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 3] + "..."


def reconstruct_timeline(traces: list[Any]) -> tuple[list[TimelineEvent], int]:
    events: list[TimelineEvent] = []
    skipped_no_output = 0
    prev_user_input = ""
    for trace in traces:
        raw_input = get_field(trace, "input")
        raw_output = get_field(trace, "output")
        output_text = extract_first_output_message(raw_output)
        if not output_text:
            skipped_no_output += 1
            continue

        input_text = extract_last_user_message(raw_input)
        timestamp = str(
            get_field(trace, "timestamp") or get_field(trace, "createdAt") or ""
        )
        name = str(get_field(trace, "name") or "unknown")
        model = str(
            get_field(trace, "model") or get_field(trace, "modelName") or "unknown"
        )

        lower_input = input_text.lower()
        if (
            lower_input.startswith("you are")
            or lower_input.startswith("<role>")
            or lower_input.startswith("## task")
        ) and len(input_text) > 500:
            input_text = "(task delegation prompt)"

        truncated_input = truncate(input_text)
        truncated_output = truncate(output_text)
        if truncated_input == prev_user_input and truncated_input:
            continue
        prev_user_input = truncated_input

        events.append(
            TimelineEvent(
                timestamp=timestamp,
                name=name,
                model=model,
                user_input=truncated_input,
                assistant_output=truncated_output,
            )
        )

    if len(events) > MAX_TIMELINE_EVENTS:
        head = events[:10]
        tail = events[-40:]
        middle_pool = events[10:-40]
        step = max(1, len(middle_pool) // 50)
        middle = middle_pool[::step][:50]
        events = head + middle + tail

    return events, skipped_no_output


def render_timeline(events: list[TimelineEvent]) -> str:
    lines: list[str] = []
    for idx, event in enumerate(events, start=1):
        lines.append(f"[{idx}] {event.timestamp} | {event.name} | {event.model}")
        if event.user_input:
            lines.append(f"  user: {event.user_input}")
        lines.append(f"  assistant: {event.assistant_output}")
    return "\n".join(lines)


def build_judge_messages(session_id: str, timeline_text: str) -> list[dict[str, str]]:
    rubric = textwrap.dedent(
        """
        You are an expert evaluator for AI coding assistant sessions.

        Score this session on:
        1) task_completion (0.0-1.0): Whether the assistant completed the user's goals.
        2) approach_quality (0.0-1.0): Soundness, efficiency, and engineering best practices.
        3) communication (0.0-1.0): Clarity, concision, and appropriateness.
        4) overall (0.0-1.0): Weighted composite (task_completion 50%, approach_quality 30%, communication 20%).

        Return JSON only with this schema:
        {
          "task_completion": {"score": <number>, "reason": "<brief reason>"},
          "approach_quality": {"score": <number>, "reason": "<brief reason>"},
          "communication": {"score": <number>, "reason": "<brief reason>"},
          "overall": {"score": <number>, "reason": "<brief reason>"}
        }

        Rules:
        - Base scores only on the provided condensed timeline.
        - Keep each reason to one short sentence.
        - Output strict JSON with double-quoted keys.
        """
    ).strip()

    content = f"Session ID: {session_id}\n\nCondensed timeline:\n{timeline_text}"
    return [
        {"role": "system", "content": rubric},
        {"role": "user", "content": content},
    ]


def call_judge(
    session_id: str, timeline_text: str, verbose: bool
) -> dict[str, Any] | None:
    import importlib

    OpenAI = importlib.import_module("openai").OpenAI
    client = OpenAI(
        base_url=LITELLM_BASE_URL,
        api_key=os.environ.get("LITELLM_KEY") or LITELLM_API_KEY,
    )
    headers = {
        "langfuse_session_id": f"eval-{session_id}",
        "langfuse_trace_name": "session-eval",
        "langfuse_tags": "eval,session-eval",
    }
    try:
        response = client.chat.completions.create(
            model=LITELLM_MODEL,
            temperature=0,
            messages=build_judge_messages(session_id, timeline_text),
            extra_headers=headers,
        )
    except Exception as exc:
        print(f"Error: LiteLLM judge call failed: {exc}", file=sys.stderr)
        return None

    raw = response.choices[0].message.content if response.choices else ""
    if not raw:
        print(
            "Warning: Judge returned empty content; skipping score post.",
            file=sys.stderr,
        )
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        raw = "\n".join(lines).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print(
            "Warning: Judge response was not valid JSON; skipping score post.",
            file=sys.stderr,
        )
        if verbose:
            print(raw)
        return None
    return parsed


def coerce_score_block(obj: dict[str, Any], key: str) -> tuple[float, str] | None:
    block = obj.get(key)
    if not isinstance(block, dict):
        return None
    raw_score = block.get("score")
    if raw_score is None:
        return None
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        return None
    score = max(0.0, min(1.0, score))
    reason = str(block.get("reason") or "")
    return score, reason


def post_scores(
    langfuse: Any, session_id: str, scores: dict[str, Any], verbose: bool
) -> int:
    dimensions = ["task_completion", "approach_quality", "communication", "overall"]
    posted = 0

    host = os.environ.get("LANGFUSE_HOST", DEFAULT_LANGFUSE_HOST).rstrip("/")
    auth = base64.b64encode(
        f"{os.environ['LANGFUSE_PUBLIC_KEY']}:{os.environ['LANGFUSE_SECRET_KEY']}".encode()
    ).decode("ascii")
    scores_url = f"{host}/api/public/scores"
    auth_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {auth}",
    }

    for dim in dimensions:
        block = coerce_score_block(scores, dim)
        if block is None:
            print(
                f"Warning: Missing/invalid score block for '{dim}', skipping.",
                file=sys.stderr,
            )
            continue
        value, comment = block

        payload: dict[str, Any] = {
            "sessionId": session_id,
            "name": dim,
            "value": value,
            "dataType": "NUMERIC",
            "comment": comment,
        }
        config_id = SCORE_CONFIGS.get(dim)
        if config_id:
            payload["configId"] = config_id
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            scores_url,
            data=body,
            method="POST",
            headers=auth_headers,
        )

        try:
            with urllib.request.urlopen(request, timeout=15) as resp:
                if 200 <= resp.status < 300:
                    posted += 1
                else:
                    print(
                        f"Warning: Score post failed for {dim} (status {resp.status}).",
                        file=sys.stderr,
                    )
        except urllib.error.URLError as exc:
            print(f"Warning: Score post failed for {dim}: {exc}", file=sys.stderr)

    debug(verbose, f"Posted {posted} session scores")
    return posted


def print_summary(
    session_id: str,
    total_traces: int,
    timeline_events: int,
    skipped_no_output: int,
    scores: dict[str, Any] | None,
    posted_scores: int,
    dry_run: bool,
) -> None:
    print("Session Evaluation")
    print("------------------")
    print(f"session_id       : {session_id}")
    print(f"traces_fetched   : {total_traces}")
    print(f"timeline_events  : {timeline_events}")
    print(f"skipped_no_output: {skipped_no_output}")
    print(f"mode             : {'dry-run' if dry_run else 'live'}")

    if scores:
        for key in ["task_completion", "approach_quality", "communication", "overall"]:
            block = coerce_score_block(scores, key)
            if not block:
                continue
            value, reason = block
            print(f"{key:16}: {value:.3f}  {reason}")
    else:
        print("scores           : n/a")

    if not dry_run:
        print(f"scores_posted    : {posted_scores}")


def main() -> int:
    args = parse_args()

    try:
        langfuse = build_langfuse_client(args.verbose)
    except Exception as exc:
        print(f"Error: could not initialize Langfuse client: {exc}", file=sys.stderr)
        return 1

    try:
        traces = fetch_traces(langfuse, args.session_id, args.verbose)
    except Exception as exc:
        print(f"Error: failed to fetch traces from Langfuse: {exc}", file=sys.stderr)
        return 1

    timeline, skipped_no_output = reconstruct_timeline(traces)
    timeline_text = render_timeline(timeline)

    scores: dict[str, Any] | None = None
    posted = 0

    if not timeline:
        debug(args.verbose, "No timeline events — skipping judge call")
    else:
        if args.verbose or args.dry_run:
            print("Condensed Timeline")
            print("------------------")
            print(timeline_text)

        if not args.dry_run:
            scores = call_judge(args.session_id, timeline_text, args.verbose)
            if scores is not None:
                try:
                    posted = post_scores(
                        langfuse, args.session_id, scores, args.verbose
                    )
                except Exception as exc:
                    print(
                        f"Warning: failed while posting scores: {exc}", file=sys.stderr
                    )

    print_summary(
        session_id=args.session_id,
        total_traces=len(traces),
        timeline_events=len(timeline),
        skipped_no_output=skipped_no_output,
        scores=scores,
        posted_scores=posted,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
