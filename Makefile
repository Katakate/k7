SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

# Use sudo only when needed
SUDO := $(shell command -v sudo >/dev/null 2>&1 && [ "$$(id -u)" -ne 0 ] && echo sudo)

# Explicit scripts (no fallbacks)
BUILD_SCRIPT := src/k7/cli/build.sh
INSTALL_SCRIPT := src/k7/cli/install.sh

# All shell scripts for linting
SH_FILES := $(shell find src/ utils/ rootfs-build/ -name '*.sh' 2>/dev/null)
# Ansible playbooks
ANSIBLE_PLAYBOOKS := src/k7/deploy/k7-install-node.yaml

.PHONY: help build install uninstall api-build-local \
        lint lint-shell lint-ansible typecheck test test-integration test-integration-remote rsync-all check

help: ## Show this help message
	@echo "Available targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

build: ## Build the k7 CLI and API into .deb package
	@echo "Running: $(BUILD_SCRIPT)"
	@$(SHELL) "$(BUILD_SCRIPT)"

install: ## Install the k7 CLI from built .deb package
	@echo "Running: $(INSTALL_SCRIPT)"
	@$(SHELL) "$(INSTALL_SCRIPT)"
	@command -v k7 >/dev/null 2>&1 && echo "Installed: $$(command -v k7)" || true

uninstall: ## Uninstall the k7 CLI
	@echo "Running: $(INSTALL_SCRIPT) uninstall"
	@$(SHELL) "$(INSTALL_SCRIPT)" uninstall
	@echo "k7 uninstalled"

api-build-local: ## Build the API container locally (dev tag)
	@echo "Building local API image: k7-api:dev"
	docker build -f src/k7/api/Dockerfile.api -t k7-api:dev .

# ── Lint ──────────────────────────────────────────────────────────
lint: ## Lint & format-check Python code (ruff)
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/

lint-shell: ## Lint shell scripts (bash -n + shellcheck)
	@echo "==> bash -n syntax check"
	@for f in $(SH_FILES); do bash -n "$$f" || exit 1; done
	@echo "==> shellcheck"
	shellcheck -S warning $(SH_FILES)

lint-ansible: ## Lint Ansible playbooks (ansible-lint)
	uv run ansible-lint --profile basic $(ANSIBLE_PLAYBOOKS)

# ── Typecheck ─────────────────────────────────────────────────────
typecheck: ## Type-check Python code (ty)
	uv run ty check src/k7

# ── Test ──────────────────────────────────────────────────────────
test: ## Run unit tests (pytest, excludes integration)
	uv run pytest

test-integration: ## Run integration tests (requires live k7 node)
	uv run pytest -m integration

test-integration-remote: ## Run integration tests on remote k7 node via SSH (set K7_NODE_IP)
	@echo "Run integration tests on the node itself: rsync this repo there, then 'make test-integration'." >&2; exit 1

rsync-all: ## Rsync repo to all nodes (K7_NODE_IPS=ip1,ip2,ip3)
	@IFS=',' read -ra IPS <<< "$${K7_NODE_IPS:-$${K7_NODE_IP:?set K7_NODE_IP to your node IP}}"; \
	for ip in "$${IPS[@]}"; do \
		echo "==> Syncing to $${K7_NODE_USER:-root}@$$ip"; \
		rsync -az --delete \
			--exclude .venv --exclude .git --exclude __pycache__ \
			--exclude '*.pyc' --exclude .ruff_cache --exclude .pytest_cache \
			--exclude .mypy_cache --exclude .coverage --exclude uv.lock \
			-e "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -i $${SSH_PRIVKEY:-$$HOME/.ssh/id_ed25519}" \
			./ "$${K7_NODE_USER:-root}@$$ip:/root/k7/"; \
	done

# ── Combined ──────────────────────────────────────────────────────
check: lint lint-shell lint-ansible typecheck test ## Run all lints + typecheck + unit tests