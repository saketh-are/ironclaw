# Sweep Summary

Result root: `/home/saketh/ironclaw/benchmarks/results/sweep-20260306T030235`

Latest run per logical case is used. The earlier invalid `hybrid-firecracker plateau 0MB` rerun is ignored in favor of `hybrid-firecracker-plateau-n5-20260306T051845`.

## Idle per-agent MiB

| Approach | n=1 | n=5 | n=10 | n=20 |
| --- | ---: | ---: | ---: | ---: |
| container-docker | 234.3 | 139.3 | 115.8 | 101.1 |
| container-gvisor-dind | 316.6 | 430.5 | 353.9 | 345.9 |
| container-sysbox-dind | 225.8 | 211.9 | 199.9 | 190.3 |
| podman-rootless | 173.5 | 177.6 | 156.5 | 129.5 |
| vm-qemu | 775.1 | 877.2 | 879.4 | 898.4 |
| hybrid-firecracker | 325.6 | 162.9 | 113.0 | 96.8 |

## Plateau 500MB

| Approach | Zero/agent MiB | First worker MiB | Steady worker MiB | r2 | Checkins | Spawn p50 ms | Cold-start p50 ms | Run |
| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- |
| container-docker | 130.3 | 500.2 | 517.5 | 1.000 | True | 441.4 | 603.9 | `container-docker-plateau-n5-20260306T030831` |
| container-gvisor-dind | 316.9 | 567.1 | 588.4 | 1.000 | True | 12291.1 | 1492.8 | `container-gvisor-dind-plateau-n5-20260306T032936` |
| container-sysbox-dind | 176.3 | 500.3 | 518.7 | 1.000 | True | 846.4 | 603.8 | `container-sysbox-dind-plateau-n5-20260306T035102` |
| podman-rootless | 149.2 | 525.6 | 512.6 | 1.000 | True | 179.0 | 583.7 | `podman-rootless-plateau-n5-20260306T041449` |
| vm-qemu | 851.4 | 467.8 | 520.8 | 1.000 | True | 519.6 | 872.4 | `vm-qemu-plateau-n5-20260306T043636` |
| hybrid-firecracker | 74.2 | 552.9 | 550.0 | 1.000 | True | 122.1 | 1983.0 | `hybrid-firecracker-plateau-n5-20260306T045621` |

## Plateau 0MB

| Approach | Zero/agent MiB | First worker MiB | Steady worker MiB | r2 | Checkins | Spawn p50 ms | Cold-start p50 ms | Run |
| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- |
| container-docker | 160.6 | 9.5 | 18.4 | 0.996 | True | 409.3 | 312.4 | `container-docker-plateau-n5-20260306T031458` |
| container-gvisor-dind | 331.7 | 49.7 | 67.5 | 0.998 | True | 12359.8 | 650.9 | `container-gvisor-dind-plateau-n5-20260306T033646` |
| container-sysbox-dind | 185.3 | -14.3 | 17.0 | 0.996 | True | 804.7 | 290.2 | `container-sysbox-dind-plateau-n5-20260306T035739` |
| podman-rootless | 138.4 | 23.0 | 11.9 | 0.943 | True | 177.4 | 290.7 | `podman-rootless-plateau-n5-20260306T042154` |
| vm-qemu | 909.9 | -4.5 | 21.0 | 0.997 | True | 515.1 | 301.7 | `vm-qemu-plateau-n5-20260306T044331` |
| hybrid-firecracker | 176.0 | 42.9 | 53.9 | 0.995 | True | 121.6 | 1167.3 | `hybrid-firecracker-plateau-n5-20260306T051845` |

