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
# sleep keeps the container alive long enough for the smoke test to observe it.
WORKER_COMMAND = os.environ.get(
    "MOCK_WORKER_COMMAND",
    "echo benchmark-worker-ok",
)


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
    has_tool_results = any(m.get("role") == "tool" for m in messages)

    if has_tool_results:
        return "text", "The command executed successfully. The benchmark worker ran as expected."

    tool_names = {
        t.get("function", {}).get("name", "")
        for t in (tools or [])
        if t.get("type") == "function"
    }

    # Prefer shell tool (direct execution, always available with ALLOW_LOCAL_TOOLS)
    if "shell" in tool_names:
        return "tool_call", ("shell", json.dumps({"command": WORKER_COMMAND}))

    # Fallback to create_job for sandbox-enabled configurations
    if "create_job" in tool_names:
        return "tool_call", ("create_job", json.dumps({
            "title": "Run benchmark worker command",
            "description": f"Execute this shell command and report the output: {WORKER_COMMAND}",
            "wait": False,
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
