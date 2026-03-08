#!/usr/bin/env python3
"""
Mock OpenAI-compatible LLM server for ironclaw benchmarks.

Returns canned responses that trigger sandbox worker spawning:
  1. On user message (no tool results): returns a `shell` tool call
  2. On follow-up (with tool results): returns a text completion

Supports both streaming (SSE) and non-streaming responses.

Usage:
    python3 mock_llm_server.py --port 11434
    # Then configure ironclaw:
    #   LLM_BACKEND=openai_compatible
    #   LLM_BASE_URL=http://127.0.0.1:11434/v1
"""

import argparse
import json
import os
import sys
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler

MOCK_MODEL = os.environ.get("MOCK_LLM_MODEL", "mock-bench")
# Command the mock LLM tells ironclaw to run in a sandbox container.
# Writes a proof file to the bind-mounted /workspace so the smoke test
# can verify storage writes actually work across the container boundary.
WORKER_COMMAND = os.environ.get(
    "MOCK_WORKER_COMMAND",
    "mkdir -p /workspace/bench-test && echo proof-$(hostname) > /workspace/bench-test/output.txt && cat /workspace/bench-test/output.txt",
)
BENCH_COMMAND_BEGIN = "<BENCH_COMMAND>"
BENCH_COMMAND_END = "</BENCH_COMMAND>"
CREATE_JOB_HINT = "Please create benchmark job."


def normalize_message_content(content):
    """Flatten OpenAI-style message content into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text") or content.get("content")
        return text if isinstance(text, str) else ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
                continue
            text = item.get("content")
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(part for part in parts if part)
    return ""


def message_has_tool_result(message):
    """Detect tool results in both OpenAI-style and IronClaw worker-style payloads."""
    if message.get("role") == "tool":
        return True
    content_text = normalize_message_content(message.get("content"))
    if content_text.startswith("[Tool `") and " returned:" in content_text:
        return True
    content = message.get("content")
    if not isinstance(content, list):
        return False
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_result":
            return True
    return False


def latest_message_content(messages):
    """Return the most recent non-empty message content."""
    for message in reversed(messages):
        content = normalize_message_content(message.get("content"))
        if content:
            return content
    return ""


def extract_requested_command(messages):
    """Extract an explicit benchmark command and preferred dispatch tool."""
    for message in reversed(messages):
        content = normalize_message_content(message.get("content"))
        if not content:
            continue
        dispatch_mode = "create_job" if CREATE_JOB_HINT in content else "shell"
        if BENCH_COMMAND_BEGIN in content and BENCH_COMMAND_END in content:
            start = content.index(BENCH_COMMAND_BEGIN) + len(BENCH_COMMAND_BEGIN)
            end = content.index(BENCH_COMMAND_END, start)
            return content[start:end].strip(), dispatch_mode
        prefix = "Please run: "
        if content.startswith(prefix):
            return content[len(prefix) :].strip(), dispatch_mode
        for line in content.splitlines():
            line = line.strip()
            if prefix in line:
                return line.split(prefix, 1)[1].strip(), dispatch_mode
            legacy = "Execute this shell command and report the output:"
            if legacy in line:
                return line.split(legacy, 1)[1].strip(), dispatch_mode
    return None, None


def _make_id():
    return f"chatcmpl-{uuid.uuid4().hex[:12]}"


def _now():
    return int(time.time())


def _usage():
    return {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------

def text_response(content, model=MOCK_MODEL):
    return {
        "id": _make_id(),
        "object": "chat.completion",
        "created": _now(),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": _usage(),
    }


def tool_call_response(tool_name, arguments_json, model=MOCK_MODEL):
    return {
        "id": _make_id(),
        "object": "chat.completion",
        "created": _now(),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": arguments_json,
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": _usage(),
    }


# ---------------------------------------------------------------------------
# Streaming helpers (Server-Sent Events)
# ---------------------------------------------------------------------------

def _stream_tool_call(tool_name, arguments_json, model=MOCK_MODEL):
    """Yield SSE chunks for a tool-call response."""
    chat_id = _make_id()
    ts = _now()
    call_id = f"call_{uuid.uuid4().hex[:8]}"

    # Chunk 1: role + tool call header (name, empty arguments)
    yield _sse_chunk(chat_id, ts, model, {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "index": 0,
            "id": call_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": ""},
        }],
    }, finish_reason=None)

    # Chunk 2: arguments payload
    yield _sse_chunk(chat_id, ts, model, {
        "tool_calls": [{"index": 0, "function": {"arguments": arguments_json}}],
    }, finish_reason=None)

    # Chunk 3: finish
    yield _sse_chunk(chat_id, ts, model, {}, finish_reason="tool_calls")
    yield "data: [DONE]\n\n"


def _stream_text(content, model=MOCK_MODEL):
    """Yield SSE chunks for a text response."""
    chat_id = _make_id()
    ts = _now()

    yield _sse_chunk(chat_id, ts, model, {
        "role": "assistant",
        "content": content,
    }, finish_reason=None)

    yield _sse_chunk(chat_id, ts, model, {}, finish_reason="stop")
    yield "data: [DONE]\n\n"


def _sse_chunk(chat_id, created, model, delta, finish_reason):
    chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    return f"data: {json.dumps(chunk)}\n\n"


# ---------------------------------------------------------------------------
# Request routing
# ---------------------------------------------------------------------------

def generate_response(messages, tools):
    """Decide what the mock LLM should return based on conversation state."""
    latest_content = latest_message_content(messages)
    if latest_content.startswith("[Job ") and (
        "Container finished" in latest_content or "Claude Code:" in latest_content
    ):
        return "text", "Acknowledged job update. No further action is required."

    has_tool_results = any(message_has_tool_result(m) for m in messages)
    requested_command, preferred_tool = extract_requested_command(messages)
    requested_command = requested_command or WORKER_COMMAND

    if has_tool_results:
        return "text", (
            "The job is complete. The benchmark worker ran successfully and "
            "produced the expected result."
        )

    tool_names = {
        t.get("function", {}).get("name", "")
        for t in (tools or [])
        if t.get("type") == "function"
    }

    if preferred_tool == "create_job" and "create_job" in tool_names:
        return "tool_call", ("create_job", json.dumps({
            "title": "Run benchmark worker command",
            "description": "\n".join([
                "Please run benchmark command.",
                BENCH_COMMAND_BEGIN,
                requested_command,
                BENCH_COMMAND_END,
            ]),
            "wait": False,
            "mode": "worker",
        }))

    # Prefer shell tool (direct execution, always available with ALLOW_LOCAL_TOOLS)
    if "shell" in tool_names:
        return "tool_call", ("shell", json.dumps({"command": requested_command}))

    # Fallback to create_job for sandbox-enabled configurations
    if "create_job" in tool_names:
        return "tool_call", ("create_job", json.dumps({
            "title": "Run benchmark worker command",
            "description": "\n".join([
                "Please run benchmark command.",
                BENCH_COMMAND_BEGIN,
                requested_command,
                BENCH_COMMAND_END,
            ]),
            "wait": False,
            "mode": "worker",
        }))

    # Fallback: no suitable tool, just return text
    return "text", "I would run a benchmark worker, but no suitable tool is available."


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class MockLLMHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/v1/models":
            self._respond_json({
                "object": "list",
                "data": [{"id": MOCK_MODEL, "object": "model", "owned_by": "mock"}],
            })
        elif self.path in ("/health", "/v1/health", "/"):
            self._respond_json({"status": "ok"})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            body = self._read_json()
            self._handle_chat_completions(body)
        else:
            self.send_error(404)

    def _handle_chat_completions(self, body):
        messages = body.get("messages", [])
        tools = body.get("tools", [])
        stream = body.get("stream", False)
        model = body.get("model", MOCK_MODEL)

        kind, payload = generate_response(messages, tools)

        if stream:
            self._send_streaming(kind, payload, model)
        else:
            self._send_non_streaming(kind, payload, model)

    def _send_non_streaming(self, kind, payload, model):
        if kind == "text":
            resp = text_response(payload, model)
        else:
            tool_name, args = payload
            resp = tool_call_response(tool_name, args, model)
        self._respond_json(resp)

    def _send_streaming(self, kind, payload, model):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        if kind == "text":
            chunks = _stream_text(payload, model)
        else:
            tool_name, args = payload
            chunks = _stream_tool_call(tool_name, args, model)

        for chunk in chunks:
            self.wfile.write(chunk.encode())
        self.wfile.flush()

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {}

    def _respond_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Structured log instead of default stderr
        pass


def main():
    parser = argparse.ArgumentParser(description="Mock OpenAI-compatible LLM server")
    parser.add_argument("--port", type=int, default=11434)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), MockLLMHandler)
    print(f"[mock-llm] Listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
