"""
Shared helpers for real-ironclaw benchmark approaches.

Contains logic common to all approach variants: environment variable
construction, health-checking the gateway, and triggering shell tool
execution via the web gateway API.
"""

import json
import time
import urllib.request

# Default ironclaw gateway auth token used in benchmark containers.
GATEWAY_AUTH_TOKEN = "bench-token"

# Image names
IRONCLAW_AGENT_IMAGE = "ironclaw-bench-agent:latest"
IRONCLAW_AGENT_DIND_IMAGE = "ironclaw-bench-agent-dind:latest"
IRONCLAW_SANDBOX_IMAGE = "ironclaw-bench-sandbox:latest"
SANDBOX_WORKER_TAR_PATH = "/tmp/ironclaw-bench-sandbox.tar"


def ironclaw_agent_env(config, agent_id, gateway_port):
    """Build the environment dict for an ironclaw benchmark container."""
    return {
        # Skip first-run onboarding wizard
        "ONBOARD_COMPLETED": "true",

        # Database — embedded libSQL, no external deps
        "DATABASE_BACKEND": "libsql",
        "LIBSQL_PATH": f"/tmp/ironclaw-{agent_id}.db",

        # LLM — point to the mock server running inside the container
        "LLM_BACKEND": "openai_compatible",
        "LLM_BASE_URL": "http://127.0.0.1:11434/v1",
        "LLM_API_KEY": "mock-key",
        "LLM_MODEL": "mock-bench",

        # Web gateway — used to trigger messages and health-check
        "GATEWAY_ENABLED": "true",
        "GATEWAY_HOST": "0.0.0.0",
        "GATEWAY_PORT": str(gateway_port),
        "GATEWAY_AUTH_TOKEN": GATEWAY_AUTH_TOKEN,
        "GATEWAY_USER_ID": "benchmark",

        # Agent tools — shell is registered as a dev tool
        "ALLOW_LOCAL_TOOLS": "true",
        # Auto-approve all tool calls (no interactive confirmation)
        "AGENT_AUTO_APPROVE_TOOLS": "true",

        # Sandbox — enabled; shell commands spawn ephemeral sandbox
        # containers via Docker, testing the isolation approach.
        "SANDBOX_ENABLED": "true",
        "SANDBOX_IMAGE": IRONCLAW_SANDBOX_IMAGE,
        "SANDBOX_AUTO_PULL": "false",
        # WorkspaceWrite: /workspace mounted rw, read-only rootfs.
        # The realistic policy for sandboxed code execution.
        "SANDBOX_POLICY": "workspace_write",

        # Disable features we don't need for benchmarking
        "CLI_ENABLED": "false",
        "HEARTBEAT_ENABLED": "false",
        "ROUTINES_ENABLED": "false",
        "SKILLS_ENABLED": "false",
        "EMBEDDING_ENABLED": "false",
        "HTTP_WEBHOOK_ENABLED": "false",

        # Agent identity
        "AGENT_NAME": f"bench-{agent_id}",

        # Logging — debug level needed to see tool call events in logs
        "RUST_LOG": "ironclaw=debug",
    }


def wait_for_gateway(port, timeout_s=120, label="agent"):
    """Poll ironclaw's /api/health endpoint until it responds 200."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=2
            )
            if resp.status == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def trigger_worker_spawn(port, auth_token=GATEWAY_AUTH_TOKEN):
    """Send a message to ironclaw's gateway that triggers a shell tool call.

    The mock LLM will respond with a `shell` tool call, causing ironclaw
    to execute the command (either directly or via sandbox depending on config).

    Returns True if the message was accepted (200/202).
    """
    url = f"http://127.0.0.1:{port}/api/chat/send"
    payload = json.dumps({
        "content": "Please run: echo benchmark-worker-ok",
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_token}",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status in (200, 202)
    except Exception as e:
        print(f"[ironclaw] Failed to trigger on :{port}: {e}")
        return False
