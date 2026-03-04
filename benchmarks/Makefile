.PHONY: images worker-image agent-image vm-image fc-kernel fc-rootfs fc-image fc-setup podman-setup run run-idle run-sweep plot compare clean help

BENCH_DIR := $(shell pwd)
APPROACH ?= container-docker
AGENTS ?= 5
MODE ?= loaded

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# --- Docker images ---

images: worker-image agent-image ## Build all Docker images

worker-image: workload/Dockerfile.worker workload/worker.py ## Build worker simulator image
	docker build -t bench-worker:latest -f workload/Dockerfile.worker workload/

agent-image: workload/Dockerfile.agent workload/agent.py ## Build agent simulator image
	docker build -t bench-agent:latest -f workload/Dockerfile.agent workload/

# --- VM image (Approach A only) ---

vm-image: images ## Build Alpine VM image with Docker (requires libguestfs)
	chmod +x vm/build-image.sh
	cd vm && bash build-image.sh

# --- Firecracker (Approach C: hybrid-firecracker) ---

fc-kernel: ## Download Firecracker kernel
	chmod +x firecracker/fetch-kernel.sh
	bash firecracker/fetch-kernel.sh

fc-rootfs: images ## Build Firecracker worker rootfs (requires root for loopback mount)
	chmod +x firecracker/build-rootfs.sh
	bash firecracker/build-rootfs.sh

fc-image: workload/Dockerfile.agent-fc workload/agent.py ## Build Firecracker-enabled agent image
	docker build -t bench-agent-fc:latest -f workload/Dockerfile.agent-fc workload/

fc-setup: fc-kernel fc-rootfs fc-image ## Full Firecracker setup (kernel + rootfs + image)

# --- Podman rootless ---

podman-setup: images ## Install podman + deps for podman-rootless approach
	@command -v podman >/dev/null || (echo "Install podman first: sudo apt install podman uidmap systemd-container"; exit 1)
	@command -v machinectl >/dev/null || (echo "Install systemd-container: sudo apt install systemd-container"; exit 1)
	@echo "Podman ready. User setup happens automatically during benchmark run."

# --- Run benchmarks ---

run: images ## Run benchmark: make run APPROACH=container-docker AGENTS=5 [MODE=idle]
	python3 -m runner.orchestrate --approach $(APPROACH) --agents $(AGENTS) --mode $(MODE)

run-idle: images ## Run idle benchmark (no workers): make run-idle APPROACH=container-docker AGENTS=50
	python3 -m runner.orchestrate --approach $(APPROACH) --agents $(AGENTS) --mode idle

run-sweep: images ## Run idle sweep at multiple N: make run-sweep APPROACH=container-docker
	@for n in 1 5 10 50 100; do \
		echo "=== Sweep: N=$$n ==="; \
		python3 -m runner.orchestrate --approach $(APPROACH) --agents $$n --mode idle; \
	done

# --- Analysis ---

plot: ## Generate charts from results/
	python3 -m analysis.plot

compare: ## Print comparison table from results/
	python3 -m analysis.compare

# --- Cleanup ---

clean: ## Remove images, VMs, Firecracker artifacts, and results
	-docker rm -f $$(docker ps -a --filter "label=bench_role" --format '{{.Names}}') 2>/dev/null
	-docker rm -f $$(docker ps -a --filter "name=bench-" --format '{{.Names}}') 2>/dev/null
	-docker rmi -f bench-worker:latest bench-agent:latest bench-agent-fc:latest 2>/dev/null
	-bash vm/launch.sh stop-all 2>/dev/null
	rm -rf results/*/
	rm -f vm/alpine-agent.qcow2 vm/.alpine-base.qcow2 vm/.worker-image.tar
	rm -f firecracker/vmlinux-5.10.bin firecracker/worker-rootfs.ext4
	@echo "Clean complete."
