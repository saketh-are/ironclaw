# Agent Isolation Benchmarks

Synthetic benchmark for isolation strategies in multi-agent deployments with sub-agent spawning.

A trivial "agent" is deployed which spawns sandboxed "workers" on a random basis. Both the agents and workers use some RAM and confirm they can write to storage. Workers check-in with their parent agent after spawn via network callback, mimicking IronClaw's architecture.

Setup, reproduction steps, and output-format details live in [USAGE.md](USAGE.md).

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

All data below was collected on a 2x 36-core, 512 GiB bare-metal host. Equivalent results were reproduced on GCP VMs with nested virtualization.

| Approach | Agent Mem | Worker Mem | Agent CPU | Worker CPU | Worker Latency |
|---|---:|---:|---:|---:|---:|
| `container-docker` | 92.7 | 18.4 | 0.0 | 0.6 | 836 |
| `container-gvisor-dind` | 339.1 | 67.5 | 76.0 | 246.6 | 8116 |
| `container-sysbox-dind` | 187.2 | 17.0 | 0.0 | 0.8 | 1205 |
| `podman-rootless` | 124.4 | 11.9 | 0.0 | 0.6 | 751 |
| `hybrid-firecracker` | 82.0 | 53.9 | 0.0 | 0.3 | 2109 |
| `vm-qemu` | 904.4 | 21.0* | 55.9 | 16.0 | 1045 |

Sysbox provides competitive performance in all metrics while offering nested containerization. In the "sibling" approaches the agent's access to a Host runtime daemon represents an attack surface which is non-trivial to harden. VM per-agent adds significant memory overhead while sibling microVMs add excessive worker latency.

## Results (loaded mode, 5 agents)

Test parameters: `SPAWN_INTERVAL_MEAN_S=5`, `WORKER_DURATION=30s`, `MAX_CONCURRENT_WORKERS=5`, `BENCHMARK_DURATION_S=180`, `RNG_SEED=42`.

| Approach | Net Mean (MiB) | Peak (MiB) | p95 (MiB) | Per-Agent (MiB) | Workers Spawned | Avg Workers | Time-to-checkin p50/p95 (ms) | Checkins OK |
|----------|---------------:|-----------:|----------:|----------------:|----------------:|------------:|------------------------------:|------------:|
| `container-docker` | 11227 | 13462 | 13364 | 2245 | 118 | 19.1 | 836 / 940 | 118/118 |
| `container-gvisor-dind` | 10581 | 12213 | 11704 | 2116 | 83 | 13.8 | 8116 / 8503 | 83/83 |
| `container-sysbox-dind` | 11566 | 13388 | 13198 | 2313 | 120 | 19.4 | 1205 / 1289 | 120/120 |
| `podman-rootless` | 10635 | 13472 | 12777 | 2127 | 125 | 19.2 | 751 / 784 | 125/125 |
| `hybrid-firecracker` | 11413 | 14092 | 13747 | 2283 | 115 | 19.2 | 2109 / 2136 | 115/115 |
| `vm-qemu` | 17489 | 17625 | 17573 | 3498 | 117 | 19.1 | 1045 / 1567 | 117/117 |

Notes:
- `container-gvisor-dind`: fewer workers spawned because inner `container.create()` still takes multiple seconds on this host, which materially eats into the `5s` mean spawn interval.

### Results (loaded mode, 50 agents)

| Approach | Net Mean (MiB) | Peak (MiB) | p95 (MiB) | Per-Agent (MiB) | Workers Spawned | Avg Workers | Time-to-checkin p50/p95 (ms) | Checkins OK |
|----------|---------------:|-----------:|----------:|----------------:|----------------:|------------:|------------------------------:|------------:|
| `container-docker` | 101284 | 119767 | 112977 | 2026 | 1131 | 179.7 | 1323 / 4534 | 1131/1131 |
| `container-gvisor-dind` | 13412 | 47492 | 34221 | 268 | 928 | 19.4 | 5182 / 8022 | 928/928 |
| `container-sysbox-dind` | 97564 | 113196 | 112227 | 1951 | 1127 | 167.5 | 1738 / 3632 | 1127/1127 |
| `podman-rootless` | failed | failed | failed | failed | failed | failed | failed | failed |
| `hybrid-firecracker` | 110515 | 127185 | 120664 | 2210 | 1172 | 185.0 | 2069 / 2249 | 1172/1172 |
| `vm-qemu` | 172451 | 172938 | 172904 | 3449 | 1181 | 190.7 | 864 / 1741 | 1181/1181 |

## Decomposed Overhead

The fixed-per-agent and marginal-per-worker figures above come from a local idle sweep (`N=1,5,10,20`, `BENCHMARK_DURATION_S=60`) paired with plateau runs at `AGENTS=5`, schedule `0,1,2,3,4,5`, `PLATEAU_HOLD_S=60`, `PLATEAU_SETTLE_S=20`, and `WORKER_MEMORY_MB=500` / `0`. Reproduction details are in [USAGE.md](USAGE.md#decomposing-agent-vs-worker-overhead).

| Approach | Agent Fixed MiB | Worker Runtime Tax MiB |
|----------|----------------:|-----------------------:|
| `container-docker` | 92.7 | 18.4 |
| `container-gvisor-dind` | 339.1 | 67.5 |
| `container-sysbox-dind` | 187.2 | 17.0 |
| `podman-rootless` | 124.4 | 11.9 |
| `hybrid-firecracker` | 82.0 | 53.9 |
| `vm-qemu` | 904.4 | 21.0* |
