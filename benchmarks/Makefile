.PHONY: images worker-image agent-image agent-dind-image agent-sysbox-image vm-image fc-kernel fc-rootfs fc-image fc-setup podman-setup run run-idle run-plateau run-sweep plot compare decompose clean help

-include config.env
.EXPORT_ALL_VARIABLES:

BENCH_DIR := $(shell pwd)
VM_ASSETS_DIR := approaches/vm_qemu_assets
FC_ASSETS_DIR := approaches/hybrid_firecracker_assets
APPROACH ?= container-docker
AGENTS ?= 5
MODE ?= loaded

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# --- Docker images ---

images: worker-image agent-image agent-dind-image agent-sysbox-image ## Build all Docker images

worker-image: workload/Dockerfile.worker workload/worker.py ## Build worker simulator image
	docker build -t bench-worker:latest -f workload/Dockerfile.worker workload/

agent-image: workload/Dockerfile.agent workload/agent.py ## Build agent simulator image
	docker build -t bench-agent:latest -f workload/Dockerfile.agent workload/

agent-dind-image: workload/Dockerfile.agent-dind workload/agent.py workload/agent-dind-entrypoint.sh ## Build DinD agent image (gVisor approach)
	docker build -t bench-agent-dind:latest -f workload/Dockerfile.agent-dind workload/

agent-sysbox-image: workload/Dockerfile.agent-sysbox workload/agent.py workload/agent-sysbox-entrypoint.sh ## Build Sysbox DinD agent image
	docker build -t bench-agent-sysbox:latest -f workload/Dockerfile.agent-sysbox workload/

# --- VM image (Approach A only) ---

vm-image: images ## Build Alpine VM image with Docker (requires libguestfs)
	chmod +x $(VM_ASSETS_DIR)/build-image.sh
	cd $(VM_ASSETS_DIR) && bash build-image.sh

# --- Firecracker (Approach C: hybrid-firecracker) ---

fc-kernel: ## Download Firecracker kernel
	chmod +x $(FC_ASSETS_DIR)/fetch-kernel.sh
	bash $(FC_ASSETS_DIR)/fetch-kernel.sh

fc-rootfs: images ## Build Firecracker worker rootfs (requires root for loopback mount)
	chmod +x $(FC_ASSETS_DIR)/build-rootfs.sh
	bash $(FC_ASSETS_DIR)/build-rootfs.sh

fc-image: workload/Dockerfile.agent-fc workload/agent.py ## Build Firecracker-enabled agent image
	docker build -t bench-agent-fc:latest -f workload/Dockerfile.agent-fc workload/

fc-setup: fc-kernel fc-rootfs fc-image ## Full Firecracker setup (kernel + rootfs + image)

# --- Podman rootless ---

podman-setup: images ## Install podman + deps for podman-rootless approach
	@command -v podman >/dev/null || (echo "Install podman first: sudo apt install podman uidmap systemd-container"; exit 1)
	@command -v machinectl >/dev/null || (echo "Install systemd-container: sudo apt install systemd-container"; exit 1)
	@echo "Podman ready. User setup happens automatically during benchmark run."

# --- Run benchmarks ---

run: images ## Run benchmark: make run APPROACH=container-docker AGENTS=5 [MODE=loaded|idle|plateau]
	python3 -m runner.orchestrate --approach $(APPROACH) --agents $(AGENTS) --mode $(MODE)

run-idle: images ## Run idle benchmark (no workers): make run-idle APPROACH=container-docker AGENTS=50
	python3 -m runner.orchestrate --approach $(APPROACH) --agents $(AGENTS) --mode idle

run-plateau: images ## Run plateau benchmark: make run-plateau APPROACH=container-docker AGENTS=5 PLATEAU_WORKERS_PER_AGENT=0,1,2,3,4,5
	python3 -m runner.orchestrate --approach $(APPROACH) --agents $(AGENTS) --mode plateau

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

decompose: ## Fit per-agent idle slope and print plateau worker costs
	python3 -m analysis.decompose

# --- Cleanup ---

clean: ## Remove images, VMs, Firecracker artifacts, and results
	-docker rm -f $$(docker ps -a --filter "label=bench_role" --format '{{.Names}}') 2>/dev/null
	-docker rm -f $$(docker ps -a --filter "name=bench-" --format '{{.Names}}') 2>/dev/null
	-docker rmi -f bench-worker:latest bench-agent:latest bench-agent-dind:latest bench-agent-sysbox:latest bench-agent-fc:latest 2>/dev/null
	-bash $(VM_ASSETS_DIR)/launch.sh stop-all 2>/dev/null
	rm -rf results/*/
	rm -f $(VM_ASSETS_DIR)/alpine-agent.qcow2 $(VM_ASSETS_DIR)/.alpine-base.qcow2 $(VM_ASSETS_DIR)/.worker-image.tar
	rm -f $(FC_ASSETS_DIR)/vmlinux-5.10.bin $(FC_ASSETS_DIR)/worker-rootfs.ext4
	@echo "Clean complete."
