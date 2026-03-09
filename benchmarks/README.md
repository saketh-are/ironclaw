# Agent Isolation Benchmarks

Benchmark suite for isolation strategies in multi-agent deployments with sub-agent spawning.

The repo now supports a real IronClaw benchmark mode with host-verified worker lifecycle evidence, alongside the older synthetic benchmark used for the reference tables below and the decomposition workflow.

Setup, reproduction steps, and output-format details live in [USAGE.md](USAGE.md).

For live topology monitoring during a synthetic run, pass `MONITOR=1` to the
`make run` targets or `--monitor` / `--monitor-port` to `bench.py synthetic`.

## Approaches

| Approach | Agent-to-Host isolation | Worker-to-Host isolation | Worker-to-Parent-Agent isolation |
|---|---|---|---|
| `container-docker` | Docker container on host `dockerd` | Docker container on host `dockerd` | Sibling Docker container boundary on the same host daemon |
| `container-gvisor-dind` | One outer `runsc`/gVisor sandbox per agent | Inner Docker container inside the agent's gVisor sandbox | Inner Docker namespaces/cgroups |
| `container-sysbox-dind` | One outer Sysbox container per agent | Inner Docker container inside the agent's Sysbox boundary | Inner Docker namespaces/cgroups |
| `podman-rootless` | Rootless Podman container under a dedicated unprivileged host user | Rootless Podman container under that same dedicated host user | Sibling rootless container; shares the parent agent's network namespace |
| `vm-qemu` | One QEMU/KVM VM per agent | Inner Docker container inside that VM | Inner Docker namespaces/cgroups |
| `hybrid-firecracker` | Docker container on host; not VM-grade, and granted `KVM`/`NET_ADMIN` access | Firecracker microVM (`KVM`) | Firecracker microVM boundary |

**Daemon model:** `container-docker` = shared host `dockerd`; `container-gvisor-dind` / `container-sysbox-dind` / `vm-qemu` = per-agent inner `dockerd`; `podman-rootless` = per-user socket-activated Podman service; `hybrid-firecracker` = no worker container daemon (agents spawn Firecracker VMs directly).

## Key Metrics

All data below was collected on a 2x 36-core, 512 GiB bare-metal host.

| Approach | Agent Mem | Worker Mem | Agent CPU | Worker CPU | Worker Latency |
|---|---:|---:|---:|---:|---:|
| `container-docker` | 107.6 | 28.5 | 0.0 | 0.7 | 924 |
| `container-gvisor-dind` | 326.2 | 67.9 | 70.7 | 373.7 | 14220 |
| `container-sysbox-dind` | 175.0 | 20.3 | 1.7 | 0.4 | 1025 |
| `podman-rootless` | 236.4 | 34.7 | 3.3 | 0.4 | 817 |
| `hybrid-firecracker` | 103.3 | 55.8 | 0.0 | 0.3 | 2210 |
| `vm-qemu` | 897.0 | 21.6 | 43.1 | 21.7 | 1082 |

Sysbox provides competitive performance in all metrics while offering nested containerization. In the "sibling" approaches the agent's access to a Host runtime daemon represents an attack surface which is non-trivial to harden. VM per-agent adds significant memory overhead while sibling microVMs add excessive worker latency.

## Results (loaded mode, 5 agents)

Test parameters: `SPAWN_INTERVAL_MEAN_S=5`, `WORKER_DURATION=30s`, `MAX_CONCURRENT_WORKERS=5`, `BENCHMARK_DURATION_S=180`, `RNG_SEED=42`.

| Approach | Net Mean (MiB) | Peak (MiB) | p95 (MiB) | Agent+Workers / Agent (MiB) | Workers Spawned | Avg Workers | Time-to-checkin p50 &#124; p95 (ms) | Checkins OK |
|----------|---------------:|-----------:|----------:|----------------:|----------------:|------------:|------------------------------:|------------:|
| `container-docker` | 11114 | 13958 | 13327 | 2223 | 120 | 19.4 | 924 &#124; 1020 | 120/120 |
| `container-gvisor-dind` | 7137 | 9124 | 8379 | 1427 | 57 | 9.8 | 14220 &#124; 16060 | 57/57 |
| `container-sysbox-dind` | 11002 | 13605 | 13030 | 2200 | 119 | 19.5 | 1025 &#124; 1103 | 119/119 |
| `podman-rootless` | 11331 | 14058 | 13511 | 2266 | 119 | 19.3 | 817 &#124; 887 | 119/119 |
| `hybrid-firecracker` | 10725 | 13428 | 13188 | 2145 | 115 | 19.1 | 2210 &#124; 2261 | 115/115 |
| `vm-qemu` | 17326 | 17408 | 17396 | 3465 | 115 | 18.9 | 1082 &#124; 2059 | 115/115 |

Notes:
- `container-gvisor-dind`: fewer workers spawned because inner `container.create()` still takes multiple seconds on this host, which materially eats into the `5s` mean spawn interval.

## Decomposed Overhead

The fixed-per-agent and marginal-per-worker figures above come from a local idle sweep (`N=1,5,10,20`, `BENCHMARK_DURATION_S=60`) paired with plateau runs at `AGENTS=5`, schedule `0,1,2,3,4,5`, `PLATEAU_HOLD_S=60`, `PLATEAU_SETTLE_S=20`, and `WORKER_MEMORY_MB=500` / `0`. Reproduction details are in [USAGE.md](USAGE.md#decomposing-agent-vs-worker-overhead).

| Approach | Agent Fixed MiB | Worker Runtime Tax MiB |
|----------|----------------:|-----------------------:|
| `container-docker` | 107.6 | 28.5 |
| `container-gvisor-dind` | 326.2 | 67.9 |
| `container-sysbox-dind` | 175.0 | 20.3 |
| `podman-rootless` | 236.4 | 34.7 |
| `hybrid-firecracker` | 103.3 | 55.8 |
| `vm-qemu` | 897.0 | 21.6 |
