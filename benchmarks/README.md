# Agent Isolation Benchmarks

Synthetic benchmark for multi-agent deployments. A trivial "agent" is deployed which spawns sandboxed "workers" on a random basis. Both the agents and workers use some RAM and confirm they can write to storage. Worker checks in with the parent agent after spawn via network callback to validate that the setup will work for IronClaw.

Setup, reproduction steps, and output-format details live in [USAGE.md](USAGE.md).

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

## Decision Summary

All tables below use local bare-metal runs on a 2x 36-core, 512 GiB host. The committed reference dataset lives under `results/baremetal-xeon6554s/`. A separate spot-check on GCP was directionally consistent, but only the bare-metal data is included here.

| Approach | Agent Mem | Worker Mem | Agent CPU | Worker CPU | Worker Latency |
|---|---:|---:|---:|---:|---:|
| `container-docker` | 92.7 | 18.4 | 0.0 | 0.6 | 836 |
| `container-gvisor-dind` | 339.1 | 67.5 | 76.0 | 246.6 | 8116 |
| `container-sysbox-dind` | 187.2 | 17.0 | 0.0 | 0.8 | 1205 |
| `podman-rootless` | 124.4 | 11.9 | 0.0 | 0.6 | 751 |
| `hybrid-firecracker` | 82.0 | 53.9 | 0.0 | 0.3 | 2109 |
| `vm-qemu` | 904.4 | 21.0* | 55.9 | 16.0 | 1045 |

Notes:
- `Agent Mem` / `Worker Mem` are memory taxes in MiB, not totals including the benchmark's intentional `500 MB` worker payload.
- `Agent CPU` is the idle-fit CPU slope in `mCPU/agent`.
- `Worker CPU` is the loaded benchmark's incremental CPU above that agent baseline, normalized by average active workers, in `mCPU/worker`.
- `Worker Latency` is loaded time-to-checkin `p50` in milliseconds.
- Very small CPU fits are rounded to `0.0`; these approaches are effectively at the noise floor in this benchmark.
- `*` `vm-qemu` shows a small marginal worker memory tax on this host, but it is still dominated by the much larger fixed per-agent VM cost.

Current takeaway: `podman-rootless` is the best pure-performance option on this host. If we want nested Docker semantics with a cleaner isolation story than the shared host `docker.sock` model, and without the Podman-specific proxy/shared-network caveat, `container-sysbox-dind` is the best compromise.

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

Regenerate with `make compare`. Detailed create/start/post-start breakdown remains available in the `Spawn Latency Detail` section of the compare output.

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

Note: `*` `vm-qemu`'s local worker slope is positive on this host, but still small relative to the much larger fixed per-agent VM tax.
