# Agent Isolation Benchmarks

Synthetic benchmark for multi-agent deployments. A trivial "agent" is deployed which spawns sandboxed "workers" on a random basis. Both the agents and workers use some RAM and confirm they can write to storage. Worker checks-in with parent agent after spawn via network callback to validate that the setup will work for IronClaw. 

## Approaches

| Approach | Agent-to-Host isolation | Worker-to-Host isolation | Worker-to-Parent-Agent isolation |
|---|---|---|---|
| `container-docker` | Docker container on shared host `dockerd`; weakened by mounted host `docker.sock` | Docker container on shared host `dockerd` | Sibling Docker container boundary on the same host daemon |
| `container-gvisor-dind` | One outer `runsc`/gVisor sandbox per agent | Inner Docker container inside the agent's shared gVisor sandbox | Inner Docker namespaces/cgroups inside the same outer gVisor sandbox |
| `container-sysbox-dind` | One outer Sysbox container per agent | Inner Docker container inside the agent's shared Sysbox boundary | Inner Docker namespaces/cgroups inside the same outer Sysbox boundary |
| `podman-rootless` | Rootless Podman container under a dedicated unprivileged host user | Rootless Podman container under that same dedicated host user | Sibling rootless container; shares the parent agent's network namespace |
| `vm-qemu` | One QEMU/KVM VM per agent | Inner Docker container inside that VM | Inner Docker namespaces/cgroups inside the same VM |
| `hybrid-firecracker` | Docker container on host; not VM-grade, and granted `KVM`/`NET_ADMIN` access | Firecracker microVM (`KVM`) | Firecracker microVM boundary; parent agent manages its lifecycle from outside |

Notes:
- In `container-gvisor-dind` and `container-sysbox-dind`, workers do not get their own separate gVisor/Sysbox sandbox. They share the parent agent's outer boundary.
- In `podman-rootless`, the worker/agent boundary is weaker than a fully separate container boundary because workers are intentionally launched with `network_mode=container:<agent>`.

**Daemon model:** `container-docker` = shared host `dockerd`; `container-gvisor-dind` / `container-sysbox-dind` / `vm-qemu` = per-agent inner `dockerd`; `podman-rootless` = per-user socket-activated Podman service; `hybrid-firecracker` = no worker container daemon (agents spawn Firecracker VMs directly).

## Results (loaded mode, 5 agents)

Test parameters: `SPAWN_INTERVAL_MEAN_S=5`, `WORKER_DURATION=30s`,
`MAX_CONCURRENT_WORKERS=5`, `BENCHMARK_DURATION_S=180`, `RNG_SEED=42`.
GCP `n2-standard-16`.

| Approach | Net Mean (MiB) | Peak (MiB) | p95 (MiB) | Per-Agent (MiB) | Workers Spawned | Avg Workers | Checkins OK |
|----------|---------------|------------|-----------|----------------|----------------|-------------|-------------|
| `container-docker` | 9893 | 13298 | 12944 | 1979 | 119 | 17.4 | 119/119 |
| `container-gvisor-dind` | 7333 | 10599 | 10082 | 1467 | 73 | — | 73/73 |
| `container-sysbox-dind` | 6315 | 13508 | 13371 | 1263 | 61 | — | 61/61 |
| `podman-rootless` | 2251 | 8105 | 5612 | 450 | 89 | 3.3 | 89/89 |
| `hybrid-firecracker` | 8979 | 14404 | 13291 | 1796 | 108 | 15.9 | 108/108 |
| `vm-qemu` | 15703 | 17554 | 17546 | 3141 | 114 | — | 111/113 |

Notes:
- `container-gvisor-dind` / `container-sysbox-dind` / `vm-qemu`: Avg workers not reported (inner daemon not sampled from host; accurate counts come from agent JSONL logs).
- `vm-qemu` checkins: 2 workers spawned near shutdown missed their checkin callback (111/113).

Spawn latency (ms):

| Approach | Create p50 | Create p95 | Start p50 | Start p95 | Total p50 | Total p95 | Cold-Start p50 | Cold-Start p95 |
|----------|-----------|-----------|----------|----------|----------|----------|---------------|---------------|
| `container-docker` | 33 | 40 | 136 | 168 | 170 | 207 | 542 | 572 |
| `container-gvisor-dind` | 6086 | 18554 | 309 | 3275 | 6459 | 18829 | 902 | 1204 |
| `container-sysbox-dind` | 56 | 1424 | 346 | 873 | 402 | 3062 | 544 | 579 |
| `podman-rootless` | 36 | 4738 | 113 | 2860 | 155 | 9185 | 719 | 959 |
| `vm-qemu` | 65 | 110 | 369 | 667 | 431 | 889 | 419 | 2889 |

Regenerate with `make compare`.

## Quick Start

```bash
# Build Docker images (required for all approaches)
make images

# Run the container approach with 5 agents (stochastic workload)
make run APPROACH=container-docker AGENTS=5

# Run idle mode (no workers — measures pure isolation overhead)
make run-idle APPROACH=container-docker AGENTS=50

# Run an idle sweep at multiple scales
make run-sweep APPROACH=container-docker

# Compare results
make compare

# Generate charts (requires matplotlib)
make plot
```

### gVisor DinD (requires runsc)

```bash
# Each agent runs in its own gVisor sandbox with a private Docker daemon
make run APPROACH=container-gvisor-dind AGENTS=3
```

Requires `runsc` runtime registered in `/etc/docker/daemon.json` with
`--net-raw` and `--allow-packet-socket-write` flags (for the DinD variant).

### Sysbox DinD (requires sysbox-runc)

```bash
# Each agent runs in its own Sysbox container with a private Docker daemon
make run APPROACH=container-sysbox-dind AGENTS=3
```

Requires `sysbox-runc` runtime registered in `/etc/docker/daemon.json`.
Uses overlay2 storage driver and native iptables (faster boot than gVisor DinD).

### Podman rootless (requires podman + systemd-container)

```bash
make podman-setup
make run APPROACH=podman-rootless AGENTS=3
```

Creates ephemeral OS users per agent. Requires `podman`, `uidmap`, and
`systemd-container` (for `machinectl` / `systemd-run --machine`).

### VM approach (requires KVM + libguestfs)

```bash
# Build the VM image (one-time)
make vm-image

# Run
make run APPROACH=vm-qemu AGENTS=5
```

### Firecracker hybrid (requires firecracker + KVM)

```bash
make fc-setup
make run APPROACH=hybrid-firecracker AGENTS=3
```

Agents run in Docker containers with `/dev/kvm` passthrough. Workers are
Firecracker microVMs spawned directly by the agent.

### GCP VM Setup

```bash
# Create a GCP VM with nested virtualization
gcloud compute instances create bench-vm \
  --zone=us-central1-a \
  --machine-type=n2-standard-16 \
  --enable-nested-virtualization \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=50GB

# SSH in and run setup
gcloud compute ssh bench-vm
sudo bash benchmarks/setup-gcp.sh
```

## Prerequisites

- **All approaches**: Linux, Docker daemon, Python 3.8+, `docker` Python SDK
- **gVisor DinD**: `runsc` runtime registered in Docker daemon config
- **Sysbox DinD**: `sysbox-runc` runtime registered in Docker daemon config
- **Podman rootless**: `podman`, `uidmap`, `systemd-container`
- **VM approach**: QEMU (`qemu-system-x86_64`), KVM (`/dev/kvm`), libguestfs-tools
- **Firecracker hybrid**: `firecracker` binary, KVM (`/dev/kvm`)
- **Charts**: `pip install matplotlib`
- **GCP**: Use `setup-gcp.sh` to install everything

## Modes

| Mode | Description | Use case |
|------|-------------|----------|
| `loaded` | Stochastic workload — agents spawn workers randomly (default) | Realistic memory profile under load |
| `idle` | No workers — agents sit idle | Measure pure isolation overhead per agent |

```bash
# Explicit mode selection
make run APPROACH=container-docker AGENTS=5 MODE=loaded
make run APPROACH=container-docker AGENTS=100 MODE=idle
```

## Configuration

Edit `config.env` to tune parameters:

```
RNG_SEED=42                   # Base seed for reproducible randomness
BENCHMARK_DURATION_S=300      # How long to run (seconds)
SPAWN_INTERVAL_MEAN_S=30      # Mean time between worker spawns per agent
MAX_CONCURRENT_WORKERS=5      # Max workers per agent
WORKER_MEMORY_MB=500          # Memory each worker allocates
WORKER_DURATION_MIN_S=30      # Min worker lifetime
WORKER_DURATION_MAX_S=120     # Max worker lifetime
```

## Host Tuning

For accurate measurements, run `setup-gcp.sh` or manually apply:

```bash
# Disable transparent huge pages
echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled

# Disable kernel same-page merging
echo 0 | sudo tee /sys/kernel/mm/ksm/run

# Disable swap
sudo swapoff -a

# Drop page caches
echo 3 | sudo tee /proc/sys/vm/drop_caches
```

The orchestrator will warn at startup if swap is enabled, and will
report if any swap activity occurred during the benchmark.

## Output

Each run creates a directory under `results/`:

```
results/container-docker-loaded-n5-20260304T143022/
├── params.json        # Full configuration for reproducibility
├── timeseries.jsonl   # Memory samples (one JSON object per line)
├── summary.json       # Aggregated statistics
├── agent-0.jsonl      # Agent event log (worker_start/worker_end events)
├── agent-1.jsonl      # ...
└── ...
```

### What's in the JSONL

Each sample includes:
- Host memory consumed (MemTotal - MemAvailable)
- Full `/proc/meminfo` breakdown (Cached, Slab, AnonPages, Shmem, Swap, etc.)
- Per-agent RSS and PSS from `/proc/<pid>/smaps_rollup`
- Daemon (dockerd, containerd) RSS and PSS
- Host CPU counters from `/proc/stat` (cumulative jiffies)
- Per-agent and per-daemon CPU counters from `/proc/<pid>/stat` (utime/stime)
- Active worker count
- Swap activity counters (pswpin/pswpout)
- Memory pressure (PSI) if available

### Summary statistics

- Baseline-subtracted mean, peak, and p50/p95/p99
- Per-agent mean overhead
- Memory drift slope (KiB/s) to detect leaks
- Daemon overhead breakdown (with baseline delta to isolate agent-caused growth)
- Host CPU utilization (%), per-agent CPU seconds, per-daemon CPU seconds
- Total workers spawned and max concurrent

## Worker Lifecycle

The agent uses the Docker Python SDK to match ironclaw's `ContainerJobManager` path:

```
create → start → wait (background) → remove
```

Each worker container gets labels for tracking and cleanup:
- `bench_run_id`: unique per benchmark run
- `bench_role`: "agent" or "worker"
- `bench_agent_id`: which agent spawned it
- `bench_approach`: which approach is running

## Adding New Approaches

1. Create `approaches/my_approach.py`
2. Implement a class extending `approaches.base.Approach`
3. Run: `make run APPROACH=my-approach AGENTS=5`

See `approaches/base.py` for the interface and `approaches/container_docker.py`
for a reference implementation.
