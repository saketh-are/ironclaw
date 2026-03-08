"""
Shared helpers for real-ironclaw benchmark approaches.

Contains logic common to all approach variants: environment variable
construction, health-checking the gateway, and triggering shell tool
execution via the web gateway API.
"""

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Default ironclaw gateway auth token used in benchmark containers.
GATEWAY_AUTH_TOKEN = "bench-token"

# Image names
IRONCLAW_AGENT_IMAGE = "ironclaw-bench-agent:latest"
IRONCLAW_AGENT_DIND_IMAGE = "ironclaw-bench-agent-dind:latest"
IRONCLAW_AGENT_FC_IMAGE = "ironclaw-bench-agent-fc:latest"
IRONCLAW_SANDBOX_IMAGE = "ironclaw-bench-sandbox:latest"
SANDBOX_WORKER_TAR_PATH = "/tmp/ironclaw-bench-sandbox.tar"
BENCH_COMMAND_BEGIN = "<BENCH_COMMAND>"
BENCH_COMMAND_END = "</BENCH_COMMAND>"


def prepare_agent_host_dirs(config, agent_id):
    """Create host-visible directories for one benchmark agent."""
    if not config.run_dir:
        raise RuntimeError("BenchmarkConfig.run_dir is required for real IronClaw verification")

    agent_root = Path(config.run_dir) / "agents" / agent_id
    workspace_dir = agent_root / "workspace"
    base_dir = agent_root / "ironclaw"
    evidence_dir = agent_root / "evidence"

    for path in (agent_root, workspace_dir, base_dir, evidence_dir):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o777)

    return {
        "agent_root": agent_root,
        "workspace_dir": workspace_dir,
        "base_dir": base_dir,
        "evidence_dir": evidence_dir,
        "agent_log_path": evidence_dir / "agent-events.jsonl",
    }


def ironclaw_agent_env(config, agent_id, gateway_port):
    """Build the environment dict for an ironclaw benchmark container."""
    env = {
        # Skip first-run onboarding wizard
        "ONBOARD_COMPLETED": "true",

        # Database — embedded libSQL, no external deps
        "DATABASE_BACKEND": "libsql",
        # Use a fixed benchmark-only master key so containers never probe the
        # host keychain or secret service during startup.
        "SECRETS_MASTER_KEY": (
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        ),

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
        "BENCH_AGENT_ID": agent_id,
        "BENCH_RUN_ID": config.run_id,

        # Logging — debug level needed to see tool call events in logs
        "RUST_LOG": "ironclaw=debug",
    }

    # Allow sandbox workers to reach the benchmark monitor through the
    # network proxy for emoji check-in POSTs.
    extra_domains = getattr(config, "sandbox_extra_domains", "")
    if extra_domains:
        env["SANDBOX_EXTRA_DOMAINS"] = extra_domains

    if os.environ.get("MOCK_WORKER_COMMAND"):
        env["MOCK_WORKER_COMMAND"] = os.environ["MOCK_WORKER_COMMAND"]

    return env


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


def gateway_get_json(path, port, auth_token=GATEWAY_AUTH_TOKEN, timeout=10):
    """Fetch JSON from the web gateway."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"Authorization": f"Bearer {auth_token}"},
        method="GET",
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read())


def gateway_read_text(path, port, auth_token=GATEWAY_AUTH_TOKEN, timeout=10):
    """Fetch a plain-text response from the web gateway."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"Authorization": f"Bearer {auth_token}"},
        method="GET",
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp.read().decode()


def list_jobs(port, auth_token=GATEWAY_AUTH_TOKEN, timeout=10):
    """Return the jobs list from a benchmark agent's gateway."""
    data = gateway_get_json("/api/jobs", port, auth_token=auth_token, timeout=timeout)
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
        return data["jobs"]
    if isinstance(data, list):
        return data
    return []


def count_active_jobs(port, auth_token=GATEWAY_AUTH_TOKEN, timeout=10):
    """Count jobs that are still pending or in progress."""
    jobs = list_jobs(port, auth_token=auth_token, timeout=timeout)
    return sum(1 for job in jobs if job.get("state") in ("pending", "in_progress"))

def get_job_detail(port, job_id, auth_token=GATEWAY_AUTH_TOKEN, timeout=10):
    """Fetch detailed job metadata for one job."""
    return gateway_get_json(
        f"/api/jobs/{job_id}",
        port,
        auth_token=auth_token,
        timeout=timeout,
    )


def get_job_events(port, job_id, auth_token=GATEWAY_AUTH_TOKEN, timeout=10):
    """Fetch persisted worker/bridge events for one job."""
    data = gateway_get_json(
        f"/api/jobs/{job_id}/events",
        port,
        auth_token=auth_token,
        timeout=timeout,
    )
    return data.get("events", []) if isinstance(data, dict) else []


def read_job_file(port, job_id, path, auth_token=GATEWAY_AUTH_TOKEN, timeout=10):
    """Read a file from a sandbox job's project workspace."""
    encoded = urllib.parse.quote(path, safe="/")
    data = gateway_get_json(
        f"/api/jobs/{job_id}/files/read?path={encoded}",
        port,
        auth_token=auth_token,
        timeout=timeout,
    )
    if isinstance(data, dict):
        return data.get("content")
    return None


def _render_benchmark_message(command, dispatch_mode):
    if command:
        return "\n".join([
            "Please create benchmark job." if dispatch_mode == "worker-job" else "Please run benchmark command.",
            BENCH_COMMAND_BEGIN,
            command,
            BENCH_COMMAND_END,
        ])
    if dispatch_mode == "worker-job":
        return "Please create benchmark job."
    return "Please run: echo benchmark-worker-ok"


def trigger_worker_spawn(
    port,
    command=None,
    auth_token=GATEWAY_AUTH_TOKEN,
    dispatch_mode="shell",
):
    """Send a benchmark message that triggers a sandbox execution path.

    dispatch_mode:
      - ``shell``: main agent calls the sandboxed shell tool directly
      - ``worker-job``: main agent creates a worker-mode sandbox job
    """
    url = f"http://127.0.0.1:{port}/api/chat/send"
    payload = json.dumps({
        "content": _render_benchmark_message(command, dispatch_mode),
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
