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
| `container-docker` | 11225 | 13819 | 13384 | 2245 | 121 | 19.3 | 121/121 |
| `container-gvisor-dind` | 10793 | 13516 | 12539 | 2159 | 104 | 17.7 | 104/104 |
| `container-sysbox-dind` | 11310 | 14018 | 13519 | 2262 | 119 | 19.2 | 119/119 |
| `podman-rootless` | 10280 | 12149 | 12010 | 2056 | 123 | 16.7 | 123/123 |
| `hybrid-firecracker` | 9515 | 13102 | 12406 | 1903 | 107 | 19.4 | 107/107 |
| `vm-qemu` | 17544 | 17578 | 17566 | 3509 | 115 | 19.3 | 114/115 |

Notes:
- `container-gvisor-dind`: Fewer workers spawned because inner `container.create()` still takes about `3.3s-3.7s`, which materially eats into the `5s` mean spawn interval.
- `vm-qemu`: One worker started right as shutdown began and never emitted a `checkin`, so the corrected ratio is `114/115`.

Spawn latency (ms):

| Approach | Create p50 | Create p95 | Start p50 | Start p95 | Total p50 | Total p95 | Cold-Start p50 | Cold-Start p95 |
|----------|-----------|-----------|----------|----------|----------|----------|---------------|---------------|
| `container-docker` | 23 | 29 | 114 | 140 | 138 | 177 | 389 | 401 |
| `container-gvisor-dind` | 3308 | 3657 | 203 | 248 | 3503 | 3883 | 606 | 747 |
| `container-sysbox-dind` | 39 | 53 | 276 | 339 | 318 | 377 | 392 | 403 |
| `podman-rootless` | 24 | 47 | 88 | 111 | 113 | 168 | 426 | 448 |
| `hybrid-firecracker` | n/a | n/a | n/a | n/a | 112 | 116 | 4706 | 4808 |
| `vm-qemu` | 46 | 103 | 352 | 727 | 397 | 824 | 405 | 3195 |

Regenerate with `make compare`.

## Results (loaded mode, 5 agents — bare-metal Xeon)

Same test parameters as above, run on a bare-metal dual-socket Intel Xeon Gold
6554S (144 threads, 503 GiB RAM) with Ubuntu 22.04 (kernel 6.8).

| Approach | Net Mean (MiB) | Peak (MiB) | p95 (MiB) | Per-Agent (MiB) | Workers Spawned | Avg Workers | Checkins OK |
|----------|---------------|------------|-----------|----------------|----------------|-------------|-------------|
| `container-docker` | 11227 | 13462 | 13364 | 2245 | 118 | 19.1 | 118/118 |
| `container-gvisor-dind` | 10581 | 12213 | 11704 | 2116 | 83 | 13.8 | 83/83 |
| `container-sysbox-dind` | 11566 | 13388 | 13198 | 2313 | 120 | 19.4 | 120/120 |
| `podman-rootless` | 10635 | 13472 | 12777 | 2127 | 125 | 19.2 | 125/125 |
| `hybrid-firecracker` | 11413 | 14092 | 13747 | 2283 | 115 | 19.2 | 115/115 |
| `vm-qemu` | 17489 | 17625 | 17573 | 3498 | 117 | 19.1 | 117/117 |

Spawn latency (ms):

| Approach | Create p50 | Create p95 | Start p50 | Start p95 | Total p50 | Total p95 | Cold-Start p50 | Cold-Start p95 |
|----------|-----------|-----------|----------|----------|----------|----------|---------------|---------------|
| `container-docker` | 57 | 78 | 196 | 259 | 252 | 351 | 580 | 610 |
| `container-gvisor-dind` | 6662 | 7112 | 315 | 359 | 6986 | 7440 | 1102 | 1326 |
| `container-sysbox-dind` | 67 | 89 | 553 | 601 | 621 | 671 | 578 | 614 |
| `podman-rootless` | 23 | 31 | 148 | 168 | 172 | 194 | 579 | 602 |
| `hybrid-firecracker` | n/a | n/a | n/a | n/a | 121 | 123 | 1989 | 2015 |
| `vm-qemu` | 66 | 124 | 386 | 522 | 452 | 613 | 552 | 952 |

### Differences from GCP reference

The bare-metal Xeon and GCP `n2-standard-16` (Ice Lake, 16 vCPUs, 64 GiB)
produce broadly consistent memory-per-agent numbers — within ~5% for most
approaches — confirming that the benchmark is measuring isolation overhead rather
than host-specific artifacts.

Key differences:

- **gVisor DinD spawn latency is ~2x slower** (6.7s vs 3.3s `create` p50).
  The `runsc` container-creation path is CPU-bound and serialized; the Xeon's
  lower single-thread turbo clock (3.0 GHz base vs ~3.5 GHz on GCP Ice Lake)
  amplifies this. The result is only 83 workers spawned vs 104 on GCP, and a
  correspondingly lower average concurrency (13.8 vs 17.7).
- **Sysbox DinD `start` latency is ~2x higher** (553ms vs 276ms p50) for the
  same clock-speed reason, though `create` remains fast enough that total
  throughput (120 workers) is on par with GCP (119).
- **container-docker `create` latency is higher** (57ms vs 23ms) — likely due
  to the higher core count creating more scheduler contention on the host
  `dockerd`. Total throughput is still comparable (118 vs 121).
- **Firecracker cold-start is faster** (2.0s vs 4.7s) because the bare-metal
  host has direct KVM access without nested virtualization overhead.
- **vm-qemu** memory is nearly identical (~3500 MiB/agent on both), confirming
  that QEMU's fixed memory allocation dominates. Cold-start latency is lower
  (552ms vs 405ms p50) because the inner Docker daemon benefits from the host's
  larger page cache.
- **100% checkins** on all approaches (vs 114/115 on GCP `vm-qemu`), likely due
  to the bare-metal host having more headroom during the shutdown window.

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
- **VM approach**: QEMU (`qemu-system-x86_64`), KVM (`/dev/kvm` accessible to the benchmark user or run as `root`), libguestfs-tools, `genisoimage` or `mkisofs`
- **Firecracker hybrid**: `firecracker` binary, KVM (`/dev/kvm`)
- **Charts**: `pip install matplotlib`
- **GCP**: Use `setup-gcp.sh` to install everything

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

Decomposition results (March 6, 2026, `n2-standard-16`):

| Approach | Agent Fixed MiB | Worker Runtime Tax MiB | Worker Payload Tax MiB | Worker Total MiB | First Worker MiB |
|----------|----------------:|-----------------------:|-----------------------:|-----------------:|-----------------:|
| `container-docker` | 87.0 | 20.9 | 501.2 | 522.1 | 521.1 |
| `container-gvisor-dind` | 313.6 | 70.0 | 492.5 | 562.5 | 584.9 |
| `container-sysbox-dind` | 174.6 | 20.4 | 500.0 | 520.4 | 485.8 |
| `podman-rootless` | 118.7 | 13.8 | 588.1 | 602.0 | 498.1 |
| `hybrid-firecracker` | 79.2 | 54.9 | 512.9 | 567.8 | 573.4 |
| `vm-qemu` | 1018.8 | ~0* | 528.5 | 528.5 | 349.2 |

Notes:

- `plateau` schedules must start at `0` and be non-decreasing.
- `plateau` forces `WORKER_LIFETIME_MODE=hold` so workers stay alive for the full stage.
- The orchestrator releases all agents into plateau mode through the agent HTTP control endpoint after collection starts, so the stages align across backends.
- `loaded` remains the realism benchmark. Use `plateau` for decomposition, not for headline density numbers.
- The table above uses an `idle` sweep at `N=1,5,10,20` (`BENCHMARK_DURATION_S=60`) plus paired `plateau` runs at `5` agents with schedule `0,1,2,3,4,5`, `PLATEAU_HOLD_S=60`, `PLATEAU_SETTLE_S=20`, and `WORKER_MEMORY_MB=500` / `0`.
- `Agent Fixed MiB` comes from the idle-fit slope, not from a single run.
- `Worker Runtime Tax MiB` comes from the `WORKER_MEMORY_MB=0` plateau slope; `Worker Payload Tax MiB` is the remainder to reach the `500 MB` plateau slope.
- `First Worker MiB` is the observed `0 -> 1 worker/agent` jump in the `500 MB` plateau run and captures one-time warm/cache effects that the steady slope smooths out.
- `*` `vm-qemu`'s zero-payload worker slope fit was `-4.9 MiB/worker`; interpret that as measurement noise around zero, not a real negative memory cost.

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
