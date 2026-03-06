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

**Daemon model:** `container-docker` = shared host `dockerd`; `container-gvisor-dind` / `container-sysbox-dind` / `vm-qemu` = per-agent inner `dockerd`; `podman-rootless` = per-user socket-activated Podman service; `hybrid-firecracker` = no worker container daemon (agents spawn Firecracker VMs directly).

## Decision Summary

All results presented in this README were collected on a bare-metal host (2x 36-core CPU, 512 GB RAM). They were largely consistent with independent testing on a GCP `n2-standard-16` instance with nested virtualization.

| Approach | Agent Mem | Worker Mem | Loaded Spawned / Avg | Ready p50/p95 |
|---|---:|---:|---:|---:|
| `container-docker` | 92.7 | 18.4 | 118 / 19.1 | 836 / 940 |
| `container-gvisor-dind` | 339.1 | 67.5 | 83 / 13.8 | 8116 / 8503 |
| `container-sysbox-dind` | 187.2 | 17.0 | 120 / 19.4 | 1205 / 1289 |
| `podman-rootless` | 124.4 | 11.9 | 125 / 19.2 | 751 / 784 |
| `hybrid-firecracker` | 82.0 | 53.9 | 115 / 19.2 | 2109 / 2136 |
| `vm-qemu` | 904.4 | 21.0* | 117 / 19.1 | 1045 / 1567 |

Notes:
- `Agent Mem` / `Worker Mem` are memory taxes in MiB, not totals including the benchmark's intentional `500 MB` worker payload.
- `Ready` is launch -> first worker checkin.

## Results (loaded mode, 5 agents)

Test parameters: `SPAWN_INTERVAL_MEAN_S=5`, `WORKER_DURATION=30s`,
`MAX_CONCURRENT_WORKERS=5`, `BENCHMARK_DURATION_S=180`, `RNG_SEED=42`.

| Approach | Net Mean (MiB) | Peak (MiB) | p95 (MiB) | Per-Agent (MiB) | Workers Spawned | Avg Workers | Checkins OK |
|----------|---------------|------------|-----------|----------------|----------------|-------------|-------------|
| `container-docker` | 11227 | 13462 | 13364 | 2245 | 118 | 19.1 | 118/118 |
| `container-gvisor-dind` | 10581 | 12213 | 11704 | 2116 | 83 | 13.8 | 83/83 |
| `container-sysbox-dind` | 11566 | 13388 | 13198 | 2313 | 120 | 19.4 | 120/120 |
| `podman-rootless` | 10635 | 13472 | 12777 | 2127 | 125 | 19.2 | 125/125 |
| `hybrid-firecracker` | 11413 | 14092 | 13747 | 2283 | 115 | 19.2 | 115/115 |
| `vm-qemu` | 17489 | 17625 | 17573 | 3498 | 117 | 19.1 | 117/117 |

Notes:
- `container-gvisor-dind`: Fewer workers spawned because inner `container.create()` still takes multiple seconds on this host, which materially eats into the `5s` mean spawn interval.

Ready latency (launch -> first checkin, ms):

| Approach | Ready p50/p95 |
|----------|---------------|
| `container-docker` | 836 / 940 |
| `container-gvisor-dind` | 8116 / 8503 |
| `container-sysbox-dind` | 1205 / 1289 |
| `podman-rootless` | 751 / 784 |
| `hybrid-firecracker` | 2109 / 2136 |
| `vm-qemu` | 1045 / 1567 |

Regenerate with `make compare`.
Detailed create/start/post-start breakdown remains available in the
`Spawn Latency Detail` section of the compare output.

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

## Prerequisites

- **All approaches**: Linux, Docker daemon, Python 3.8+, `docker` Python SDK
- **gVisor DinD**: `runsc` runtime registered in Docker daemon config
- **Sysbox DinD**: `sysbox-runc` runtime registered in Docker daemon config
- **Podman rootless**: `podman`, `uidmap`, `systemd-container`
- **VM approach**: QEMU (`qemu-system-x86_64`), KVM (`/dev/kvm` accessible to the benchmark user or run as `root`), libguestfs-tools, `genisoimage` or `mkisofs`
- **Firecracker hybrid**: `firecracker` binary, KVM (`/dev/kvm`)
- **Charts**: `pip install matplotlib`

## Modes

| Mode | Description | Use case |
|------|-------------|----------|
| `loaded` | Stochastic workload — agents spawn workers randomly (default) | Realistic memory profile under load |
| `idle` | No workers — agents sit idle | Measure pure isolation overhead per agent |
| `plateau` | Deterministic steady-state worker plateaus | Isolate per-worker overhead from per-agent overhead |

```bash
# Explicit mode selection
make run APPROACH=container-docker AGENTS=5 MODE=loaded
make run APPROACH=container-docker AGENTS=100 MODE=idle
make run-plateau APPROACH=container-docker AGENTS=5 \
  PLATEAU_WORKERS_PER_AGENT=0,1,2,3,4,5 \
  PLATEAU_HOLD_S=60 PLATEAU_SETTLE_S=20
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
WORKER_LIFETIME_MODE=timed    # timed (default) or hold
PLATEAU_WORKERS_PER_AGENT=    # e.g. 0,1,2,3,4,5 for plateau mode
PLATEAU_HOLD_S=60             # Seconds per plateau
PLATEAU_SETTLE_S=20           # Seconds to discard at plateau start
```

## Host Tuning

For accurate measurements, apply:

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
- Plateau-mode zero-point, first-worker tax, steady-worker slope, and per-stage points

## Decomposing Agent vs Worker Overhead

Use two runs, not one:

1. `idle` sweep to fit fixed per-agent overhead.
2. `plateau` run to fit marginal per-worker overhead at steady state.

Recommended sequence:

```bash
# Fixed per-agent overhead
for n in 1 5 10 20; do
  make run-idle APPROACH=container-docker AGENTS=$n BENCHMARK_DURATION_S=60
done

# Per-worker steady-state overhead
make run-plateau APPROACH=container-docker AGENTS=5 \
  PLATEAU_WORKERS_PER_AGENT=0,1,2,3,4,5 \
  PLATEAU_HOLD_S=60 PLATEAU_SETTLE_S=20

# Optional: isolation/runtime tax without worker payload
make run-plateau APPROACH=container-docker AGENTS=5 \
  PLATEAU_WORKERS_PER_AGENT=0,1,2,3,4,5 \
  PLATEAU_HOLD_S=60 PLATEAU_SETTLE_S=20 \
  WORKER_MEMORY_MB=0

# Fit the decomposition from results/
make decompose
```

Decomposition results (March 6, 2026, local bare-metal Xeon Gold 6554S):

| Approach | Agent Fixed MiB | Worker Runtime Tax MiB |
|----------|----------------:|-----------------------:|
| `container-docker` | 92.7 | 18.4 |
| `container-gvisor-dind` | 339.1 | 67.5 |
| `container-sysbox-dind` | 187.2 | 17.0 |
| `podman-rootless` | 124.4 | 11.9 |
| `hybrid-firecracker` | 82.0 | 53.9 |
| `vm-qemu` | 904.4 | 21.0* |

Notes:

- `plateau` schedules must start at `0` and be non-decreasing.
- `plateau` forces `WORKER_LIFETIME_MODE=hold` so workers stay alive for the full stage.
- The orchestrator releases all agents into plateau mode through the agent HTTP control endpoint after collection starts, so the stages align across backends.
- `loaded` remains the realism benchmark. Use `plateau` for decomposition, not for headline density numbers.
- The table above uses an `idle` sweep at `N=1,5,10,20` (`BENCHMARK_DURATION_S=60`) plus paired `plateau` runs at `5` agents with schedule `0,1,2,3,4,5`, `PLATEAU_HOLD_S=60`, `PLATEAU_SETTLE_S=20`, and `WORKER_MEMORY_MB=500` / `0`.
- `Agent Fixed MiB` comes from the idle-fit slope, not from a single run.
- `Worker Runtime Tax MiB` comes from the `WORKER_MEMORY_MB=0` plateau slope.
- Worker totals that include the benchmark's intentional `500 MB` worker allocation are omitted here; use runtime tax as the representative per-worker overhead.
- `*` `vm-qemu`'s local worker slope is positive on this host, but still small relative to the much larger fixed per-agent VM tax.

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
