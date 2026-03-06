# Benchmark Setup and Reference

This document covers setup, reproducibility, output format, and benchmark internals. For the approach summary and the current benchmark results, see [../README.md](../README.md).

The committed reference dataset is organized under `results/baremetal-xeon6554s/`. The analysis commands recurse through nested result folders, so `make compare` and `make decompose` work against that consolidated layout.

## Quick Start

```bash
# Build Docker images (required for all approaches)
make images

# Run the container approach with 5 agents (stochastic workload)
make run APPROACH=container-docker AGENTS=5

# Run idle mode (no workers; measures pure isolation overhead)
make run-idle APPROACH=container-docker AGENTS=50

# Run an idle sweep at multiple scales
make run-sweep APPROACH=container-docker

# Compare results
make compare

# Generate charts (requires matplotlib)
make plot
```

## Approach-Specific Setup

### gVisor DinD (requires runsc)

```bash
# Each agent runs in its own gVisor sandbox with a private Docker daemon
make run APPROACH=container-gvisor-dind AGENTS=3
```

Requires `runsc` runtime registered in `/etc/docker/daemon.json` with `--net-raw` and `--allow-packet-socket-write` flags for the DinD variant.

### Sysbox DinD (requires sysbox-runc)

```bash
# Each agent runs in its own Sysbox container with a private Docker daemon
make run APPROACH=container-sysbox-dind AGENTS=3
```

Requires `sysbox-runc` runtime registered in `/etc/docker/daemon.json`. Uses `overlay2` and native iptables, which boots faster than the gVisor DinD variant.

### Podman Rootless (requires podman + systemd-container)

```bash
make podman-setup
make run APPROACH=podman-rootless AGENTS=3
```

Creates ephemeral OS users per agent. Requires `podman`, `uidmap`, and `systemd-container` for `machinectl` / `systemd-run --machine`.

### VM Approach (requires KVM + libguestfs)

```bash
# Build the VM image (one time)
make vm-image

# Run
make run APPROACH=vm-qemu AGENTS=5
```

### Firecracker Hybrid (requires firecracker + KVM)

```bash
make fc-setup
make run APPROACH=hybrid-firecracker AGENTS=3
```

Agents run in Docker containers with `/dev/kvm` passthrough. Workers are Firecracker microVMs spawned directly by the agent.

## Prerequisites

- All approaches: Linux, Docker daemon, Python 3.8+, `docker` Python SDK
- gVisor DinD: `runsc` runtime registered in Docker daemon config
- Sysbox DinD: `sysbox-runc` runtime registered in Docker daemon config
- Podman rootless: `podman`, `uidmap`, `systemd-container`
- VM approach: QEMU (`qemu-system-x86_64`), KVM (`/dev/kvm` accessible to the benchmark user or run as `root`), `libguestfs-tools`, `genisoimage` or `mkisofs`
- Firecracker hybrid: `firecracker` binary, KVM (`/dev/kvm`)
- Charts: `pip install matplotlib`

## Modes

| Mode | Description | Use case |
|------|-------------|----------|
| `loaded` | Stochastic workload; agents spawn workers randomly (default) | Realistic memory profile under load |
| `idle` | No workers; agents sit idle | Measure pure isolation overhead per agent |
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

```dotenv
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

The orchestrator warns at startup if swap is enabled and reports whether any swap activity occurred during the benchmark.

## Output Format

Each run creates a directory under `results/`. For the committed reference data in this repo, those run directories live under `results/baremetal-xeon6554s/`:

```text
results/baremetal-xeon6554s/container-docker-loaded-n5-20260306T014541/
├── params.json        # Full configuration for reproducibility
├── timeseries.jsonl   # Memory samples (one JSON object per line)
├── summary.json       # Aggregated statistics
├── agent-0.jsonl      # Agent event log (worker_start/worker_end events)
├── agent-1.jsonl      # ...
└── ...
```

### What's in the JSONL

Each sample includes:
- Host memory consumed (`MemTotal - MemAvailable`)
- Full `/proc/meminfo` breakdown (`Cached`, `Slab`, `AnonPages`, `Shmem`, `Swap`, etc.)
- Per-agent RSS and PSS from `/proc/<pid>/smaps_rollup`
- Daemon (`dockerd`, `containerd`) RSS and PSS
- Host CPU counters from `/proc/stat` (cumulative jiffies)
- Per-agent and per-daemon CPU counters from `/proc/<pid>/stat` (`utime` / `stime`)
- Active worker count
- Swap activity counters (`pswpin` / `pswpout`)
- Memory pressure (PSI) if available

### Summary Statistics

`summary.json` includes:
- Baseline-subtracted mean, peak, and p50 / p95 / p99
- Per-agent mean overhead
- Memory drift slope (KiB/s) to detect leaks
- Daemon overhead breakdown (with baseline delta to isolate agent-caused growth)
- Host CPU utilization, per-agent CPU seconds, and per-daemon CPU seconds
- Total workers spawned and max concurrent
- Plateau-mode zero-point, first-worker tax, steady-worker slope, and per-stage points

## Decomposing Agent vs Worker Overhead

Use two runs, not one:

1. Run an `idle` sweep to fit fixed per-agent overhead.
2. Run a `plateau` benchmark to fit marginal per-worker overhead at steady state.

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

Notes:
- `plateau` schedules must start at `0` and be non-decreasing.
- `plateau` forces `WORKER_LIFETIME_MODE=hold` so workers stay alive for the full stage.
- The orchestrator releases all agents into plateau mode through the agent HTTP control endpoint after collection starts, so the stages align across backends.
- `loaded` remains the realism benchmark. Use `plateau` for decomposition, not for headline density numbers.
- The headline `Agent Mem` figure comes from the idle-fit slope, not from a single run.
- The headline `Worker Mem` figure comes from the `WORKER_MEMORY_MB=0` plateau slope.
- The committed sweep used for the headline decomposition lives at `results/baremetal-xeon6554s/sweep-20260306T030235/`.

## Worker Lifecycle

The agent uses the Docker Python SDK to match IronClaw's `ContainerJobManager` path:

```text
create -> start -> wait (background) -> remove
```

Each worker container gets labels for tracking and cleanup:
- `bench_run_id`: unique per benchmark run
- `bench_role`: `agent` or `worker`
- `bench_agent_id`: which agent spawned it
- `bench_approach`: which approach is running

## Adding New Approaches

1. Create `approaches/my_approach.py`.
2. Implement a class extending `approaches.base.Approach`.
3. Run `make run APPROACH=my-approach AGENTS=5`.

See `approaches/base.py` for the interface and `approaches/container_docker.py` for a reference implementation.
