# Benchmark Usage

This document defaults to the real IronClaw benchmark path in [ironclaw_benchmark.py](/home/saketh/ironclaw/benchmarks/ironclaw_benchmark.py). The legacy synthetic runner is still available, but it is now secondary and mainly useful for reproducing the older reference dataset and decomposition sweeps.

For the approach summary and current comparison tables, see [README.md](/home/saketh/ironclaw/benchmarks/README.md).

## Default Path

The default benchmark flow is:

1. Build the real IronClaw benchmark images.
2. Run `ironclaw_benchmark.py` against one of the `ironclaw-*` approaches.
3. Verify success from host-visible evidence written under the run directory.

The real benchmark now verifies these events from disk rather than trusting IronClaw job APIs:

- agent started
- agent wrote storage
- worker job created
- worker started
- worker wrote storage
- worker callback received
- worker cleanup logged and absence verified
- agent cleanup verified

## Quick Start

```bash
# Build the real benchmark images
make ironclaw-images

# Smoke test on shared Docker
make ironclaw-benchmark \
  APPROACH=ironclaw-docker \
  AGENTS=2 \
  EXTRA_ARGS="--mode loaded --max-triggers-per-agent 1 --job-profile sleep --job-duration-min-s 2 --job-duration-max-s 2"

# Staggered Sysbox run with one long-lived worker per agent
make ironclaw-benchmark \
  APPROACH=ironclaw-sysbox-dind \
  AGENTS=50 \
  EXTRA_ARGS='--mode loaded --max-concurrent-workers 1 --max-triggers-per-agent 1 --batch-size 10 --batch-interval-s 5 --job-profile sleep --job-duration-min-s 600 --job-duration-max-s 600'

# Run with the live topology monitor enabled
make run APPROACH=container-docker AGENTS=5 MONITOR=1

# Run idle mode (no workers; measures pure isolation overhead)
make run-idle APPROACH=container-docker AGENTS=50

# Compare older committed reference results
make compare
```

You can also run the real benchmark directly:

```bash
python3 ironclaw_benchmark.py \
  --approach ironclaw-docker \
  --agents 5 \
  --mode loaded
```

Available real approaches:

```bash
python3 bench.py list --suite ironclaw
```

## Real Approaches

### `ironclaw-docker`

Shared host Docker daemon. This is the simplest real benchmark topology.

```bash
make ironclaw-benchmark APPROACH=ironclaw-docker AGENTS=5
```

### `ironclaw-gvisor-dind`

Each agent runs in an outer gVisor sandbox with a private inner Docker daemon.

```bash
make ironclaw-benchmark APPROACH=ironclaw-gvisor-dind AGENTS=3
```

Requires `runsc` registered in the host Docker daemon config.

### `ironclaw-sysbox-dind`

Each agent runs in an outer Sysbox container with a private inner Docker daemon.

```bash
make ironclaw-benchmark APPROACH=ironclaw-sysbox-dind AGENTS=5
```

Requires `sysbox-runc` registered in the host Docker daemon config.

### `ironclaw-hybrid-firecracker`

Real IronClaw agents in Docker containers, with worker jobs executed inside Firecracker microVMs. This keeps the real agent/gateway path while moving worker execution onto a KVM-backed microVM boundary.

```bash
# One-time setup
sudo make ironclaw-fc-setup

# Benchmark run
make ironclaw-benchmark APPROACH=ironclaw-hybrid-firecracker AGENTS=3
```

Requires `firecracker`, `/dev/kvm`, `/dev/net/tun`, and the Firecracker kernel/rootfs assets under `approaches/hybrid_firecracker_assets/`.

### `ironclaw-podman`

Each agent gets its own rootless Podman user and user-scoped Podman service.

```bash
make podman-setup
make ironclaw-benchmark APPROACH=ironclaw-podman AGENTS=3
```

Requires `podman`, `uidmap`, and `systemd-container`.

### `ironclaw-vm-qemu`

Each agent runs inside its own QEMU/KVM VM with an inner Docker daemon.

```bash
# One-time image build
sudo env LIBGUESTFS_BACKEND=direct bash vm/build-vm-image.sh

# Benchmark run
make ironclaw-benchmark APPROACH=ironclaw-vm-qemu AGENTS=3
```

Requires `qemu-system-x86_64`, `/dev/kvm`, `virt-builder`, `virt-customize`, and `genisoimage` or `mkisofs`.

On this host, `LIBGUESTFS_BACKEND=direct` was needed to avoid a broken libguestfs `passt` path during image build.

## Modes

The real benchmark supports the same three top-level modes:

| Mode | Description | Use case |
| --- | --- | --- |
| `loaded` | host-driven stochastic triggering of real worker jobs | realistic callback-path and runtime behavior |
| `idle` | agents only, no worker jobs | fixed per-agent overhead |
| `plateau` | host-driven worker target plateaus per agent | per-worker steady-state overhead |

Examples:

```bash
# Idle
python3 ironclaw_benchmark.py \
  --approach ironclaw-docker \
  --agents 20 \
  --mode idle

# Loaded
python3 ironclaw_benchmark.py \
  --approach ironclaw-sysbox-dind \
  --agents 50 \
  --mode loaded \
  --max-concurrent-workers 1 \
  --batch-size 10 \
  --batch-interval-s 5

# Plateau
python3 ironclaw_benchmark.py \
  --approach ironclaw-docker \
  --agents 5 \
  --mode plateau \
  --plateau-workers-per-agent 0,1,2,3,4,5 \
  --plateau-hold-s 60 \
  --plateau-settle-s 20
```

## Live Monitor

The synthetic runner can serve a live webpage that shows one box per agent and
fixed worker-slot indicators per agent in a single compact view.

```bash
# Default monitor URL: http://127.0.0.1:8765/
make run APPROACH=container-docker AGENTS=5 MONITOR=1

# Custom host/port
python3 bench.py synthetic \
  --approach container-docker \
  --agents 5 \
  --monitor \
  --monitor-host 0.0.0.0 \
  --monitor-port 9000
```

The orchestrator prints the exact monitor URL when the run starts.

## Job Profiles

Real mode can inject arbitrary benchmark jobs.

Built-in profiles:

| Profile | What it does |
| --- | --- |
| `sleep` | writes a proof file, then sleeps for the requested duration |
| `memory-touch` | writes a proof file, allocates and touches resident memory, then holds it |
| `custom` | runs your own command template |

Examples:

```bash
# Built-in memory pressure job
python3 ironclaw_benchmark.py \
  --approach ironclaw-sysbox-dind \
  --agents 5 \
  --mode plateau \
  --job-profile memory-touch \
  --job-memory-mb 256

# Fully custom job payload
python3 ironclaw_benchmark.py \
  --approach ironclaw-docker \
  --agents 5 \
  --mode loaded \
  --job-profile custom \
  --job-command "mkdir -p {proof_dir} && echo hi > {proof_file} && sleep {duration_s}"
```

Template variables for `--job-command`:

- `{agent_id}`
- `{trigger_index}`
- `{duration_s}`
- `{memory_mb}`
- `{proof_dir}`
- `{proof_file}`

## Output Format

Each real benchmark run writes a directory under `results/`:

```text
results/ironclaw-docker-loaded-n5-20260308T010000/
├── params.json
├── timeseries.jsonl
├── summary.json
└── agents/
    ├── agent-0/
    │   ├── evidence/
    │   │   ├── agent-events.jsonl
    │   │   ├── job-created-<job>.json
    │   │   ├── worker-callback-<job>.json
    │   │   └── worker-cleaned-<job>.json
    │   ├── workspace/
    │   │   └── .bench-evidence/
    │   └── ironclaw/
    │       └── projects/<job-id>/
    └── ...
```

Important files:

- `params.json`: exact benchmark inputs
- `timeseries.jsonl`: memory and CPU samples
- `summary.json`: aggregated resource stats plus evidence-derived lifecycle stats
- `agents/<agent>/evidence/agent-events.jsonl`: agent start/storage/exit events
- `agents/<agent>/ironclaw/projects/<job-id>/.bench-evidence/worker-started-<job>.json`: worker-start marker
- `agents/<agent>/ironclaw/projects/<job-id>/.bench-evidence/worker-storage-written-<job>.json`: worker storage-write marker
- `agents/<agent>/ironclaw/projects/<job-id>/bench-test/output-...txt`: worker proof output

### Summary Fields

The real benchmark `summary.json` includes:

- host memory stats: `steady_state_mean_mib`, `peak_mib`, `p95_mib`
- CPU stats: `host_cpu_pct`, `per_agent_cpu_s`
- control stats: `workers_spawned`, `avg_workers`, `final_active_workers`
- evidence stats:
  - `agents_started`
  - `agents_with_storage`
  - `jobs_discovered`
  - `jobs_started`
  - `jobs_with_storage_event`
  - `jobs_with_callback_event`
  - `jobs_with_proof`
  - `jobs_cleaned`
  - `jobs_cleanup_verified`
  - `jobs_succeeded`
  - `agents_cleanup_verified`
- latency breakdowns:
  - `trigger_to_job_created`
  - `trigger_to_started`
  - `trigger_to_worker_storage`
  - `trigger_to_proof`
  - `trigger_to_callback`
  - `trigger_to_cleanup`

## Legacy Synthetic Runner

The synthetic runner is still present:

- [runner/orchestrate.py](/home/saketh/ironclaw/benchmarks/runner/orchestrate.py)
- `make run`
- `make run-idle`
- `make run-plateau`
- `make run-sweep`

Use it when you specifically need:

- compatibility with the older committed bare-metal reference dataset
- the historical synthetic decomposition sweeps in [results/baremetal-xeon6554s](/home/saketh/ironclaw/benchmarks/results/baremetal-xeon6554s)
- legacy container-vs-VM-vs-Firecracker comparisons that are still summarized in [README.md](/home/saketh/ironclaw/benchmarks/README.md)

The synthetic decomposition workflow remains:

```bash
# Fixed per-agent slope
for n in 1 5 10 20; do
  make run-idle APPROACH=container-docker AGENTS=$n BENCHMARK_DURATION_S=60
done

# Plateau worker slope
make run-plateau APPROACH=container-docker AGENTS=5 \
  PLATEAU_WORKERS_PER_AGENT=0,1,2,3,4,5 \
  PLATEAU_HOLD_S=60 PLATEAU_SETTLE_S=20

# Optional 0 MB worker tax
make run-plateau APPROACH=container-docker AGENTS=5 \
  PLATEAU_WORKERS_PER_AGENT=0,1,2,3,4,5 \
  PLATEAU_HOLD_S=60 PLATEAU_SETTLE_S=20 \
  WORKER_MEMORY_MB=0

make decompose
```

## Host Tuning

For stable measurements:

```bash
echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
echo 0 | sudo tee /sys/kernel/mm/ksm/run
sudo swapoff -a
echo 3 | sudo tee /proc/sys/vm/drop_caches
```

The collectors record swap and PSI when available, but the cleanest runs still come from a quiet host.
