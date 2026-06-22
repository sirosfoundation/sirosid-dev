# sirosid-dev Makefile
#
# Local development environment for SIROS ID wallet ecosystem.
#
# Quick Start:
#   make up               # Start default stack (go-trust allow-all PDP)
#   make up VC=1           # Add production-like VC services
#   make up PDP=deny       # Use deny-all PDP for negative testing
#   make status            # Check service health
#   make down              # Stop all services

# Truthy value check: treat 1, yes, on, up as true
_truthy = $(filter 1 yes on up,$(1))

# Wallet display name
WALLET_NAME ?= SIROS ID (dev)

.PHONY: help setup up down logs status status-vc \
        ensure-conformance-hosts fetch-golden-env \
        register-mocks register-vc-services clean show-branches show-images build-info pki \
	android-setup android-config android-up android-down android-full android-restart android-launch android-logs android-test \
	install conformance-install

# =============================================================================
# Configuration
# =============================================================================

# GitHub org
GITHUB_ORG ?= git@github.com:sirosfoundation

# Workspace paths - defaults assume sibling directories
FRONTEND_PATH ?= ../wallet-frontend
BACKEND_PATH ?= ../go-wallet-backend

# Docker compose files
PRIMARY_COMPOSE := docker-compose.test.yml
GO_TRUST_COMPOSE := docker-compose.go-trust.yml
GO_TRUST_ALLOW_COMPOSE := docker-compose.go-trust-allow.yml
GO_TRUST_WHITELIST_COMPOSE := docker-compose.go-trust-whitelist.yml
GO_TRUST_DENY_COMPOSE := docker-compose.go-trust-deny.yml
VC_SERVICES_COMPOSE := docker-compose.vc-services.yml
VC_GO_TRUST_COMPOSE := docker-compose.vc-go-trust.yml
CONFORMANCE_COMPOSE := docker-compose.conformance.yml
HTTP_TRANSPORT_COMPOSE := docker-compose.http-transport.yml
WMP_TRANSPORT_COMPOSE := docker-compose.wmp-transport.yml
R2PS_COMPOSE := docker-compose.r2ps.yml
DOMAIN_COMPOSE := docker-compose.domain.yml
GOLDEN_COMPOSE := docker-compose.golden.yml
GOLDEN_GO_TRUST_COMPOSE := docker-compose.golden-go-trust.yml
GOLDEN_VC_COMPOSE := docker-compose.golden-vc.yml

# Stack options (override on command line)
PDP ?= allow
VC ?=
TRANSPORT ?=
CONFORMANCE ?=
R2PS ?=
DOMAIN ?=
GOLDEN ?=
REBUILD ?=

# Golden release configuration
GOLDEN_RELEASES_URL := https://raw.githubusercontent.com/sirosfoundation/siros-conformance/main/golden-releases.yaml
GOLDEN_RELEASES_CACHE := .golden-releases.yaml

# Host for URL construction (localhost or DOMAIN if set)
_HOST := $(if $(DOMAIN),$(DOMAIN),localhost)

# Service URLs (published for use by sirosid-tests)
export FRONTEND_URL ?= http://$(_HOST):3000
export BACKEND_URL ?= http://$(_HOST):8080
export ENGINE_URL ?= http://$(_HOST):8082
export ADMIN_URL ?= http://$(_HOST):8081
export MOCK_VERIFIER_URL ?= http://$(_HOST):9011
export MOCK_PDP_URL ?= http://$(_HOST):9081
export VCTM_REGISTRY_URL ?= http://$(_HOST):8080/registry

# R2PS service URLs
export R2PS_URL ?= http://$(_HOST):8443
export R2PS_ADMIN_URL ?= http://$(_HOST):8444

# VC Services URLs (external, for health checks from host)
export VC_ISSUER_URL ?= http://$(_HOST):9000
export VC_VERIFIER_URL ?= http://$(_HOST):9001
export VC_APIGW_URL ?= http://$(_HOST):9003
export VC_REGISTRY_URL ?= http://$(_HOST):9004
# VC Services URLs (internal, for container-to-container registration)
VC_APIGW_INTERNAL_URL ?= http://vc-apigw:8080
VC_VERIFIER_INTERNAL_URL ?= http://vc-verifier:8080
export GO_TRUST_ALLOW_URL ?= http://$(_HOST):9095
export GO_TRUST_WHITELIST_URL ?= http://$(_HOST):9096
export GO_TRUST_DENY_URL ?= http://$(_HOST):9097

export ADMIN_TOKEN ?= e2e-test-admin-token-for-testing-purposes-only

# Colors for output
GREEN := \033[0;32m
YELLOW := \033[0;33m
RED := \033[0;31m
NC := \033[0m

# =============================================================================
# Compose file list builder
# =============================================================================
# Build the list of compose files based on PDP, VC, TRANSPORT, CONFORMANCE

COMPOSE_FILES := -f $(PRIMARY_COMPOSE)

# PDP selection
ifeq ($(PDP),allow)
  COMPOSE_FILES += -f $(GO_TRUST_COMPOSE) -f $(GO_TRUST_ALLOW_COMPOSE)
  _PDP_LABEL := go-trust allow-all
else ifeq ($(PDP),whitelist)
  COMPOSE_FILES += -f $(GO_TRUST_COMPOSE) -f $(GO_TRUST_WHITELIST_COMPOSE)
  _PDP_LABEL := go-trust whitelist
else ifeq ($(PDP),deny)
  COMPOSE_FILES += -f $(GO_TRUST_COMPOSE) -f $(GO_TRUST_DENY_COMPOSE)
  _PDP_LABEL := go-trust deny-all
else ifeq ($(PDP),mock)
  _PDP_LABEL := mock-trust-pdp
else
  $(error Unknown PDP mode '$(PDP)'. Use: allow, whitelist, deny, mock)
endif

# VC services
ifneq ($(call _truthy,$(VC)),)
  COMPOSE_FILES += -f $(VC_SERVICES_COMPOSE)
  # Note: vc-go-trust.yml is intentionally NOT included here.
  # go-trust services (allow/deny/whitelist) are already provided by
  # docker-compose.go-trust.yml via the PDP selection above. Adding
  # vc-go-trust.yml alongside go-trust.yml redefines the same services
  # with different container_name values, which breaks Docker Compose
  # validation on macOS Docker Desktop.
  _VC_LABEL := yes
else
  _VC_LABEL := no
endif

# Transport override
ifeq ($(TRANSPORT),wmp)
  COMPOSE_FILES += -f $(WMP_TRANSPORT_COMPOSE)
  _TRANSPORT_LABEL := WMP (JSON-RPC+SSE)
else ifeq ($(TRANSPORT),http)
  COMPOSE_FILES += -f $(HTTP_TRANSPORT_COMPOSE)
  _TRANSPORT_LABEL := HTTP proxy
else
  _TRANSPORT_LABEL := WebSocket (default)
endif

# Conformance suite (implies VC + allow + http transport)
ifneq ($(call _truthy,$(CONFORMANCE)),)
  # Ensure required overlays are present
  ifeq ($(findstring $(VC_SERVICES_COMPOSE),$(COMPOSE_FILES)),)
    COMPOSE_FILES += -f $(VC_SERVICES_COMPOSE)
  endif
  ifeq ($(findstring $(VC_GO_TRUST_COMPOSE),$(COMPOSE_FILES)),)
    COMPOSE_FILES += -f $(VC_GO_TRUST_COMPOSE)
  endif
  ifeq ($(findstring $(HTTP_TRANSPORT_COMPOSE),$(COMPOSE_FILES)),)
    COMPOSE_FILES += -f $(HTTP_TRANSPORT_COMPOSE)
  endif
  COMPOSE_FILES += -f $(CONFORMANCE_COMPOSE)
  _CONFORMANCE_LABEL := yes
else
  _CONFORMANCE_LABEL := no
endif

# R2PS services (WSCD/WSCA via SoftHSM2)
ifneq ($(call _truthy,$(R2PS)),)
  COMPOSE_FILES += -f $(R2PS_COMPOSE)
  _R2PS_LABEL := yes
else
  _R2PS_LABEL := no
endif

# Custom domain (replaces localhost for mobile device access)
ifneq ($(DOMAIN),)
  COMPOSE_FILES += -f $(DOMAIN_COMPOSE)
  export DOMAIN
  _DOMAIN_LABEL := $(DOMAIN)
else
  _DOMAIN_LABEL := localhost (default)
endif

# Golden release: use pre-built images instead of local builds
ifneq ($(GOLDEN),)
  # Resolve the golden release: "yes" means default, anything else is a release name
  ifeq ($(filter yes 1 on up,$(GOLDEN)),)
    _GOLDEN_RELEASE := $(GOLDEN)
  else
    _GOLDEN_RELEASE := default
  endif
  _GOLDEN_LABEL := $(_GOLDEN_RELEASE)
  COMPOSE_FILES += -f $(GOLDEN_COMPOSE)
  ifneq ($(PDP),mock)
    COMPOSE_FILES += -f $(GOLDEN_GO_TRUST_COMPOSE)
  endif
  ifneq ($(call _truthy,$(VC)),)
    # Note: VC golden images are NOT used because fixtures/vc-config.yaml
    # is written for current source. VC services build from source even
    # in golden mode. Remove this guard when golden VC images and config
    # are version-aligned.
    # COMPOSE_FILES += -f $(GOLDEN_VC_COMPOSE)
  endif
else
  _GOLDEN_LABEL :=
endif

# =============================================================================
# Help
# =============================================================================

help: ## Show this help
	@echo "$(GREEN)sirosid-dev$(NC) — Local Development Environment"
	@echo ""
	@echo "$(GREEN)Usage:$(NC)"
	@echo "  make setup             Clone sibling repos"
	@echo "  make install           Install dependencies (npm, etc.)"
	@echo "  make up [OPTIONS]    Start the stack"
	@echo "  make down            Stop all services"
	@echo "  make status          Check service health"
	@echo "  make logs            View Docker logs"
	@echo "  make clean           Remove all containers and volumes"
	@echo ""
	@echo "$(GREEN)Options:$(NC)  (pass on the make command line)"
	@echo ""
	@echo "  $(YELLOW)PDP=$(NC)             Trust PDP to use (default: $(GREEN)allow$(NC))"
	@echo "                     allow      go-trust allow-all — trusts everything (dev default)"
	@echo "                     whitelist  go-trust whitelist — trusts configured issuers only"
	@echo "                     deny       go-trust deny-all  — rejects everything (negative testing)"
	@echo "                     mock       legacy mock-trust-pdp"
	@echo ""
	@echo "  $(YELLOW)VC=$(NC)              Enable production-like VC services (default: off)"
	@echo "                     1, yes, on, up — enable"
	@echo "                     Adds: vc-issuer, vc-verifier, vc-apigw, vc-registry, mongodb"
	@echo ""
	@echo "  $(YELLOW)TRANSPORT=$(NC)       Transport protocol (default: websocket)"
	@echo "                     wmp        WMP transport (JSON-RPC + SSE)"
	@echo "                     http       HTTP proxy transport"
	@echo ""
	@echo "  $(YELLOW)CONFORMANCE=$(NC)     Enable OpenID conformance suite (default: off)"
	@echo "                     1, yes, on, up — enable"
	@echo "                     Implies: VC=1 PDP=allow TRANSPORT=http"
	@echo ""
	@echo "  $(YELLOW)R2PS=$(NC)            Enable R2PS service with SoftHSM2 (default: off)"
	@echo "                     1, yes, on, up — enable"
	@echo "                     Adds: go-r2ps-service, SoftHSM2 (WSCD + attestation)"
	@echo ""
	@echo "  $(YELLOW)DOMAIN=$(NC)          Set a custom domain (default: localhost)"
	@echo "                     Replaces localhost in all service URLs"
	@echo "                     Enables access from mobile devices on the local network"
	@echo ""
	@echo "  $(YELLOW)GOLDEN=$(NC)          Use pre-built images from a golden release (default: off)"
	@echo "                     yes        use the default golden release"
	@echo "                     <name>     use a specific release (e.g. beta_r2)"
	@echo "                     Tags are fetched from siros-conformance/golden-releases.yaml"
	@echo ""
	@echo "  $(YELLOW)REBUILD=$(NC)         Force rebuild all images with no cache (default: off)"
	@echo "                     1, yes, on — rebuild everything from scratch"
	@echo ""
	@echo "$(GREEN)Examples:$(NC)"
	@echo "  make up                              # Default: frontend + backend + go-trust allow"
	@echo "  make up VC=yes                       # Add VC issuer/verifier services"
	@echo "  make up PDP=whitelist VC=1            # VC services with whitelist trust"
	@echo "  make up PDP=deny VC=1                 # Negative testing: deny all trust"
	@echo "  make up PDP=mock                      # Legacy mock PDP (no go-trust)"
	@echo "  make up TRANSPORT=wmp                 # Use WMP transport"
	@echo "  make up CONFORMANCE=yes               # Full conformance test stack"
	@echo "  make up R2PS=yes VC=yes               # R2PS with SoftHSM2 + VC services"
	@echo "  make up DOMAIN=myhost.local            # Custom domain for mobile device access"
	@echo "  make up GOLDEN=yes                    # Use default golden release (pre-built images)"
	@echo "  make up GOLDEN=beta_r2 VC=1           # Use specific golden release with VC"
	@echo "  make up REBUILD=yes                    # Force full rebuild (no cache)"
	@echo ""
	@echo "$(GREEN)Service URLs (when running):$(NC)"
	@echo "  Frontend:      $(FRONTEND_URL)"
	@echo "  Backend API:   $(BACKEND_URL)"
	@echo "  Admin API:     $(ADMIN_URL)"
	@echo "  Engine:        $(ENGINE_URL)"
	@echo ""
	@echo "$(GREEN)Source paths:$(NC)  (override with env vars or on command line)"
	@echo ""
	@echo "  $(YELLOW)FRONTEND_PATH=$(NC)   wallet-frontend source  (default: $(GREEN)../wallet-frontend$(NC))"
	@echo "  $(YELLOW)BACKEND_PATH=$(NC)    go-wallet-backend source (default: $(GREEN)../go-wallet-backend$(NC))"
	@echo "  $(YELLOW)VC_PATH=$(NC)          vc services source     (default: $(GREEN)../vc$(NC))"
	@echo "  $(YELLOW)GO_TRUST_PATH=$(NC)    go-trust source        (default: $(GREEN)../go-trust$(NC))"
	@echo ""
	@echo "$(GREEN)Other variables:$(NC)"
	@echo ""
	@echo "  $(YELLOW)WALLET_NAME=$(NC)      Wallet display name (default: $(GREEN)SIROS ID (dev)$(NC))"
	@echo ""
	@echo "$(GREEN)Integration:$(NC)"
	@echo "  Run tests with: cd ../sirosid-tests && make test"
	@echo ""
	@echo "$(GREEN)Android SDK:$(NC)"
	@echo "  make android-setup   Generate assetlinks.json + configure ADB for passkey dev"
	@echo "  make android-config  Generate Android overlay VC config"
	@echo "  make android-up      Generate Android config + start Android overlay services"
	@echo "  make android-down    Stop Android overlay services"
	@echo "  make android-full    Full Android flow (config + build + deploy + register + launch)"
	@echo "  make android-restart Restart Android test services + relaunch app"
	@echo "  make android-launch  Launch installed sample app + log snapshot"
	@echo "  make android-logs    Follow Android app logs"
	@echo "  SDK_PACKAGE=x.y.z    Override package name (default: org.sirosfoundation.sdk.sample)"
	@echo ""

# =============================================================================
# Helpers
# =============================================================================
# Update all repos to their default upstream branches
update:
	@echo "$(GREEN)Force updating all SIROS repos...$(NC)"
	repos="sirosid-dev wallet-frontend go-wallet-backend go-trust wallet-common vc"; \
	for repo in $$repos; do \
	  branch=main; \
	  [ "$$repo" = "wallet-common" ] && branch=release/sirosid; \
	  [ "$$repo" = "wallet-frontend" ] && branch=release/sirosid; \
	  if [ -d "../$$repo/.git" ]; then \
	    echo "Updating $$repo to $$branch..."; \
	    git -C ../$$repo fetch origin; \
	    git -C ../$$repo checkout $$branch; \
	    git -C ../$$repo reset --hard origin/$$branch; \
	  fi; \
	done
	@echo "$(GREEN)All repos updated.$(NC)"
# Print the git branch for each locally-built repo
show-branches:
	@echo "$(GREEN)Local repo branches:$(NC)"
	@if [ -d "$(FRONTEND_PATH)/.git" ]; then \
		printf "  %-24s %s\n" "wallet-frontend:" "$$(git -C $(FRONTEND_PATH) branch --show-current)"; \
	fi
	@if [ -d "$(BACKEND_PATH)/.git" ]; then \
		printf "  %-24s %s\n" "go-wallet-backend:" "$$(git -C $(BACKEND_PATH) branch --show-current)"; \
	fi
	@echo ""

# Print docker images used by the running stack
show-images:
	@echo "$(GREEN)Docker images:$(NC)"
	@docker images --format 'table  {{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}' \
		--filter "reference=*-e2e-test:local" 2>/dev/null | tail -n +2 || true
	@echo ""

# Generate build-info.json with git metadata from each component repo
build-info:
	@echo '{ "components": [' > build-info.json
	@SEP=""; \
	for repo_info in \
		"wallet-frontend:$(FRONTEND_PATH)" \
		"go-wallet-backend:$(BACKEND_PATH)" \
		"sirosid-dev:."; \
	do \
		name=$${repo_info%%:*}; \
		path=$${repo_info#*:}; \
		if [ -d "$$path/.git" ]; then \
			branch=$$(git -C "$$path" branch --show-current 2>/dev/null || echo "unknown"); \
			commit=$$(git -C "$$path" rev-parse HEAD 2>/dev/null || echo "unknown"); \
			built=$$(git -C "$$path" log -1 --format='%ci' 2>/dev/null || echo "unknown"); \
			dirty="false"; \
			if ! git -C "$$path" diff --quiet HEAD 2>/dev/null; then dirty="true"; fi; \
			printf '%s\n    { "name": "%s", "branch": "%s", "commit": "%s", "built": "%s", "dirty": %s }' \
				"$$SEP" "$$name" "$$branch" "$$commit" "$$built" "$$dirty" >> build-info.json; \
			SEP=","; \
		fi; \
	done
	@echo '' >> build-info.json
	@echo '  ], "generated": "'$$(date -Iseconds)'" }' >> build-info.json
	@echo "$(GREEN)Generated build-info.json$(NC)"

# =============================================================================
# Up / Down / Status
# =============================================================================

CONFORMANCE_HOSTNAME := localhost.emobix.co.uk

up: ## Start the stack (use PDP=, VC=, TRANSPORT=, CONFORMANCE=, GOLDEN= to configure)
	@# Ensure .well-known/assetlinks.json exists (Docker bind mount requires it)
	@mkdir -p .well-known && [ -f .well-known/assetlinks.json ] || echo '[]' > .well-known/assetlinks.json
	@# Ensure the shared e2e-test-network exists
	@docker network inspect e2e-test-network >/dev/null 2>&1 || docker network create e2e-test-network
ifneq ($(call _truthy,$(VC)),)
	@# Pre-flight: ../vc must exist for VC service builds
	@if [ ! -d "$(VC_PATH:-../vc)" ] && [ ! -d "../vc" ]; then \
		echo "$(RED)Error: VC services require the 'vc' repo at ../vc$(NC)"; \
		echo "  Run: make setup   (clones all required sibling repos)"; \
		echo "  Or:  git clone https://github.com/SUNET/vc ../vc"; \
		exit 1; \
	fi
	@# Pre-flight: generate PKI if missing
	@if [ ! -f fixtures/vc-pki/rootCA.crt ]; then \
		echo "$(YELLOW)VC PKI not found — generating...$(NC)"; \
		$(MAKE) --no-print-directory pki; \
	fi
endif
ifneq ($(call _truthy,$(CONFORMANCE)),)
	@$(MAKE) --no-print-directory ensure-conformance-hosts
endif
ifneq ($(GOLDEN),)
	@$(MAKE) --no-print-directory fetch-golden-env
endif
	@echo "$(GREEN)Starting sirosid-dev...$(NC)"
	@echo "  PDP:         $(_PDP_LABEL)"
	@echo "  VC services: $(_VC_LABEL)"
	@echo "  Transport:   $(_TRANSPORT_LABEL)"
	@echo "  Conformance: $(_CONFORMANCE_LABEL)"
	@echo "  R2PS:        $(_R2PS_LABEL)"
	@echo "  Domain:      $(_DOMAIN_LABEL)"
ifneq ($(GOLDEN),)
	@echo "  Golden:      $(_GOLDEN_LABEL)"
endif
	@echo ""
ifneq ($(GOLDEN),)
	@echo "$(GREEN)Golden release images:$(NC)"
	@cat .env.golden 2>/dev/null | while IFS='=' read -r k v; do \
		printf "  %-28s %s\n" "$$k" "$$v"; \
	done
	@echo ""
else
	@$(MAKE) --no-print-directory show-branches
endif
	@$(MAKE) --no-print-directory build-info
	@echo "$(YELLOW)Building and starting containers...$(NC)"
ifneq ($(GOLDEN),)
	set -a && . ./.env.golden && set +a && \
	WALLET_NAME="$(WALLET_NAME)" \
		docker compose $(COMPOSE_FILES) up -d --pull always 2>&1 | \
		grep -E '^\s*(✔|=>|Pulling|Container|Network|Image)' || true
else
ifneq ($(call _truthy,$(REBUILD)),)
	@echo "$(YELLOW)Force-rebuilding all images (no cache)...$(NC)"
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) \
		WALLET_NAME="$(WALLET_NAME)" \
		docker compose $(COMPOSE_FILES) build --no-cache 2>&1 | \
		grep -E '^\s*(✔|=>|Building|Container|Network|Image)' || true
endif
	@_LOG=$$(mktemp /tmp/compose.XXXXXX); \
	[ -f .env.android ] && . ./.env.android && export APK_KEY_HASH || true; \
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) \
		WALLET_NAME="$(WALLET_NAME)" \
		docker compose $(COMPOSE_FILES) up -d --build >$$_LOG 2>&1; \
	_EXIT=$$?; \
	grep -E '^\s*(✔|=>|Building|Container|Network|Image)' $$_LOG || true; \
	if [ $$_EXIT -ne 0 ]; then \
		echo ""; \
		echo "$(RED)docker compose failed (exit $$_EXIT). Full output:$(NC)"; \
		cat $$_LOG; \
		rm -f $$_LOG; \
		exit $$_EXIT; \
	fi; \
	rm -f $$_LOG
endif
	@$(MAKE) --no-print-directory show-images
	@$(MAKE) --no-print-directory status
ifneq ($(call _truthy,$(VC)),)
	@$(MAKE) --no-print-directory status-vc
	@$(MAKE) --no-print-directory register-vc-services
endif
ifneq ($(call _truthy,$(CONFORMANCE)),)
	@echo ""
	@echo "$(GREEN)Waiting for conformance suite to start (this may take 60s+)...$(NC)"
	@for i in $$(seq 1 30); do \
		curl -fsk https://$(CONFORMANCE_HOSTNAME):8443/api/runner/available >/dev/null 2>&1 && break; \
		sleep 5; \
	done
	@curl -fsk https://$(CONFORMANCE_HOSTNAME):8443/api/runner/available >/dev/null 2>&1 && \
		echo "$(GREEN)✓ Conformance suite ready at https://$(CONFORMANCE_HOSTNAME):8443/$(NC)" || \
		echo "$(YELLOW)○ Conformance suite still starting... check: docker logs conformance-suite-server$(NC)"
endif
	@echo ""
	@echo "$(GREEN)Environment ready!$(NC)"
	@echo "  Frontend: $(FRONTEND_URL)"
	@echo "  Backend:  $(BACKEND_URL)"
	@echo ""

down: ## Stop all services
	@echo "$(YELLOW)Stopping all services...$(NC)"
	-docker compose $(COMPOSE_FILES) down
	@echo "$(GREEN)Done.$(NC)"

logs: ## View service logs
	docker compose $(COMPOSE_FILES) logs -f

status: ## Check core service health
	@echo "$(GREEN)Service Status:$(NC)"
	@echo ""
	@printf "  %-20s %s\n" "Service" "Status"
	@printf "  %-20s %s\n" "-------" "------"
	@curl -sf $(FRONTEND_URL) >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "wallet-frontend" "✓ running" || \
		printf "  %-20s $(RED)%s$(NC)\n" "wallet-frontend" "✗ not running"
	@curl -sf $(BACKEND_URL)/health >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "wallet-backend" "✓ running" || \
		printf "  %-20s $(RED)%s$(NC)\n" "wallet-backend" "✗ not running"
	@curl -sf $(ADMIN_URL)/admin/status >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "wallet-admin" "✓ running" || \
		printf "  %-20s $(RED)%s$(NC)\n" "wallet-admin" "✗ not running"
	@curl -sf $(GO_TRUST_ALLOW_URL)/healthz >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "go-trust-allow" "✓ running" || true
	@curl -sf $(GO_TRUST_WHITELIST_URL)/healthz >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "go-trust-whitelist" "✓ running" || true
	@curl -sf $(GO_TRUST_DENY_URL)/healthz >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "go-trust-deny" "✓ running" || true
	@curl -sf $(MOCK_PDP_URL)/health >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "mock-trust-pdp" "✓ running" || true
	@curl -sf $(MOCK_VERIFIER_URL)/health >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "mock-verifier" "✓ running" || true
	@curl -sf $(VCTM_REGISTRY_URL)/status >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "vctm-registry" "✓ running" || true
	@echo ""

status-vc: ## Check VC service health
	@echo "$(GREEN)VC Service Status:$(NC)"
	@echo ""
	@printf "  %-20s %s\n" "Service" "Status"
	@printf "  %-20s %s\n" "-------" "------"
	@curl -sf $(VC_ISSUER_URL)/health >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "vc-issuer" "✓ running" || \
		printf "  %-20s $(RED)%s$(NC)\n" "vc-issuer" "✗ not running"
	@curl -sf $(VC_VERIFIER_URL)/.well-known/openid-configuration >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "vc-verifier" "✓ running" || \
		printf "  %-20s $(RED)%s$(NC)\n" "vc-verifier" "✗ not running"
	@curl -sf $(VC_APIGW_URL)/.well-known/oauth-authorization-server >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "vc-apigw" "✓ running" || \
		printf "  %-20s $(RED)%s$(NC)\n" "vc-apigw" "✗ not running"
	@curl -sf $(VC_REGISTRY_URL)/health >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "vc-registry" "✓ running" || \
		printf "  %-20s $(RED)%s$(NC)\n" "vc-registry" "✗ not running"
	@echo ""

# =============================================================================
# Conformance helpers
# =============================================================================

ensure-conformance-hosts: ## Ensure /etc/hosts has the conformance suite entry
	@if grep -q '$(CONFORMANCE_HOSTNAME)' /etc/hosts; then \
		echo "$(GREEN)✓ /etc/hosts already has $(CONFORMANCE_HOSTNAME)$(NC)"; \
	else \
		echo "$(YELLOW)Adding 127.0.0.1 $(CONFORMANCE_HOSTNAME) to /etc/hosts (requires sudo)...$(NC)"; \
		echo '127.0.0.1 $(CONFORMANCE_HOSTNAME)' | sudo tee -a /etc/hosts >/dev/null; \
		echo "$(GREEN)✓ Added $(CONFORMANCE_HOSTNAME) to /etc/hosts$(NC)"; \
	fi

# =============================================================================
# Mock Registration
# =============================================================================

TENANT_ID ?= default

register-mocks: ## Register mock verifier with backend
	@echo "$(GREEN)Registering mock services...$(NC)"
	@curl -sf -X POST $(ADMIN_URL)/admin/tenants/$(TENANT_ID)/verifiers \
		-H "Authorization: Bearer $(ADMIN_TOKEN)" \
		-H "Content-Type: application/json" \
		-d '{"name":"Mock Verifier","url":"$(MOCK_VERIFIER_URL)"}' && \
		echo "  Mock verifier registered" || \
		echo "  $(YELLOW)Warning: Could not register mock verifier$(NC)"

register-vc-services: ## Register VC issuer and verifier with backend
	@echo "$(GREEN)Registering VC services with wallet backend...$(NC)"
	@for i in $$(seq 1 30); do \
		curl -sf $(ADMIN_URL)/admin/tenants/$(TENANT_ID) \
			-H "Authorization: Bearer $(ADMIN_TOKEN)" >/dev/null 2>&1 && break; \
		sleep 2; \
	done
	@curl -sf -X POST $(ADMIN_URL)/admin/tenants/$(TENANT_ID)/issuers \
		-H "Authorization: Bearer $(ADMIN_TOKEN)" \
		-H "Content-Type: application/json" \
		-d '{"credential_issuer_identifier":"$(VC_APIGW_INTERNAL_URL)","visible":true}' && \
		echo "  $(GREEN)✓ VC issuer registered$(NC)" || \
		echo "  $(YELLOW)Warning: Could not register VC issuer$(NC)"
	@curl -sf -X POST $(ADMIN_URL)/admin/tenants/$(TENANT_ID)/verifiers \
		-H "Authorization: Bearer $(ADMIN_TOKEN)" \
		-H "Content-Type: application/json" \
		-d '{"name":"VC Verifier","url":"$(VC_VERIFIER_INTERNAL_URL)"}' && \
		echo "  $(GREEN)✓ VC verifier registered$(NC)" || \
		echo "  $(YELLOW)Warning: Could not register VC verifier$(NC)"

# =============================================================================
# Golden Release Resolution
# =============================================================================
# Fetches golden-releases.yaml from siros-conformance and generates .env.golden
# with image env vars for docker compose.

# Service → env var / image registry mapping (matches siros-conformance)
define GOLDEN_AWK
BEGIN { in_release=0; in_images=0 }
/^default:/ { default_release=$$2 }
/^  [a-z]/ {
  sub(/:$$/, "", $$1);
  current_release=$$1;
  if (release == "default" && current_release == default_release) in_release=1;
  else if (current_release == release) in_release=1;
  else in_release=0;
  in_images=0;
}
in_release && /images:/ { in_images=1; next }
in_images && /^      [a-z]/ {
  sub(/:$$/, "", $$1);
  gsub(/^ +/, "", $$1);
  svc=$$1; tag=$$2;
  gsub(/"/, "", tag);
  if (svc == "wallet-frontend")   printf "WALLET_FRONTEND_IMAGE=ghcr.io/sirosfoundation/wallet-frontend:%s\n", tag;
  if (svc == "go-wallet-backend") printf "WALLET_BACKEND_IMAGE=ghcr.io/sirosfoundation/go-wallet-backend:%s\n", tag;
  if (svc == "go-trust")          printf "GO_TRUST_IMAGE=ghcr.io/sirosfoundation/go-trust:%s\n", tag;
  if (svc == "vc-issuer")         printf "VC_ISSUER_IMAGE=ghcr.io/sirosfoundation/vc/issuer:%s\n", tag;
  if (svc == "vc-verifier")       printf "VC_VERIFIER_IMAGE=ghcr.io/sirosfoundation/vc/verifier:%s\n", tag;
  if (svc == "vc-apigw")          printf "VC_APIGW_IMAGE=ghcr.io/sirosfoundation/vc/apigw:%s\n", tag;
  if (svc == "vc-registry")       printf "VC_REGISTRY_IMAGE=ghcr.io/sirosfoundation/vc/registry:%s\n", tag;
}
in_images && /^    [a-z]/ { in_images=0 }
endef
export GOLDEN_AWK

fetch-golden-env:
	@echo "$(GREEN)Fetching golden release ($(_GOLDEN_RELEASE))...$(NC)"
	@curl -sfL "$(GOLDEN_RELEASES_URL)" -o $(GOLDEN_RELEASES_CACHE) || \
		{ echo "$(RED)Failed to fetch golden-releases.yaml$(NC)"; exit 1; }
	@awk -v release="$(_GOLDEN_RELEASE)" "$$GOLDEN_AWK" $(GOLDEN_RELEASES_CACHE) > .env.golden
	@if [ ! -s .env.golden ]; then \
		echo "$(RED)No images found for release '$(_GOLDEN_RELEASE)'.$(NC)"; \
		echo "Available releases:"; \
		awk '/^  [a-z].*:$$/ { sub(/:$$/, "", $$1); printf "  %s\n", $$1 }' $(GOLDEN_RELEASES_CACHE); \
		rm -f .env.golden; \
		exit 1; \
	fi

# =============================================================================
# Cleanup
# =============================================================================

clean: ## Remove all containers, volumes and build cache
	@echo "$(YELLOW)Cleaning up...$(NC)"
	-docker compose $(COMPOSE_FILES) down -v --remove-orphans
	-docker compose $(COMPOSE_FILES) down -v --remove-orphans 2>/dev/null
	@echo "$(YELLOW)Pruning build cache for project images...$(NC)"
	-docker builder prune -f --filter label=com.docker.compose.project 2>/dev/null || true
	@echo "$(GREEN)Done. Run 'make up' to rebuild from scratch.$(NC)"

# =============================================================================
# PKI Generation
# =============================================================================

pki: ## Generate fresh PKI (signing keys and certificates)
	@echo "$(GREEN)Generating PKI...$(NC)"
	cd fixtures && ./create-pki.sh

# =============================================================================
# Setup — clone sibling repositories
# =============================================================================

# repo:branch pairs — override GITHUB_ORG to use a different remote
SETUP_REPOS := \
	wallet-frontend:release/sirosid \
	wallet-common:release/sirosid \
	go-wallet-backend:main \
	go-trust:main \
	vc:main

setup: ## Clone sibling repos needed for local development
	@echo "$(GREEN)Setting up sibling repositories...$(NC)"
	@for entry in $(SETUP_REPOS); do \
		repo=$${entry%%:*}; \
		branch=$${entry#*:}; \
		dir="../$${repo}"; \
		if [ -d "$$dir" ]; then \
			printf "  %-24s $(YELLOW)exists$(NC) (%s)\n" "$$repo" "$$(git -C $$dir branch --show-current 2>/dev/null || echo 'not a git repo')"; \
		else \
			echo "  Cloning $$repo (branch $$branch)..."; \
			git clone -b "$$branch" "$(GITHUB_ORG)/$$repo.git" "$$dir" && \
				printf "  %-24s $(GREEN)cloned$(NC) ($$branch)\n" "$$repo" || \
				printf "  %-24s $(RED)failed$(NC)\n" "$$repo"; \
		fi; \
	done
	@echo ""
	@echo "$(GREEN)Done.$(NC) Run 'make install' to install dependencies, then 'make up' to start the stack."

# =============================================================================
# Dependency Installation
# =============================================================================

conformance-install: ## Install conformance-runner npm dependencies
	@echo "$(GREEN)Installing conformance-runner dependencies...$(NC)"
	cd conformance-runner && npm ci
	@echo "$(GREEN)✓ conformance-runner dependencies installed$(NC)"

install: conformance-install ## Install all project dependencies
	@echo ""
	@echo "$(GREEN)All dependencies installed.$(NC)"

# =============================================================================
# R2PS Service
# =============================================================================

r2ps-setup: ## Verify R2PS service health and show status
	@./scripts/setup-r2ps.sh

# =============================================================================
# Cloudflare Tunnels (on-demand TLS domains for mobile/external testing)
# =============================================================================

TUNNEL_COMPOSE := docker-compose.tunnel.yml

tunnel: ## Start Cloudflare quick tunnels (no account needed) and show URLs
	@./scripts/tunnel.sh start

tunnel-stop: ## Stop Cloudflare tunnels and clean up
	@./scripts/tunnel.sh stop

tunnel-status: ## Show active tunnel URLs and process status
	@./scripts/tunnel.sh status

restart-with-tunnels: ## Restart the stack using Cloudflare tunnel URLs
	@if [ ! -f .env.tunnel ]; then \
		echo "$(RED)No tunnels running. Start them first: make tunnel$(NC)"; \
		exit 1; \
	fi
	@. ./.env.tunnel && \
		TUNNEL_RPID=$$(echo "$$TUNNEL_FRONTEND_URL" | sed 's|https://||') && \
		export TUNNEL_RPID TUNNEL_FRONTEND_URL TUNNEL_BACKEND_URL TUNNEL_ENGINE_URL && \
		{ [ -f .env.android ] && . ./.env.android && export APK_KEY_HASH || true; } && \
		echo "$(GREEN)Restarting with tunnel URLs...$(NC)" && \
		echo "  Frontend: $$TUNNEL_FRONTEND_URL" && \
		echo "  Backend:  $$TUNNEL_BACKEND_URL" && \
		echo "  Engine:   $$TUNNEL_ENGINE_URL" && \
		echo "  RP ID:    $$TUNNEL_RPID" && \
		[ -n "$$APK_KEY_HASH" ] && echo "  APK hash: $$APK_KEY_HASH" || true && \
		docker compose $(COMPOSE_FILES) -f $(TUNNEL_COMPOSE) up -d
	@echo ""
	@echo "$(GREEN)✓ Stack restarted with Cloudflare tunnel URLs$(NC)"
	@echo "  Open the frontend URL on any device to test"
	@echo ""
	@# Re-run android-setup so the ADB compat flag is refreshed for the new tunnel domain.
	@# trycloudflare.com subdomains are not in Google's Statement List cache, so Android's
	@# CredentialManager will reject passkey creation unless DEVELOPMENT_PASSKEY_REGISTRATION
	@# is enabled on the device — even when the tunnel uses real HTTPS.
	@$(MAKE) --no-print-directory android-setup 2>/dev/null || true
	@echo ""
	@echo "$(YELLOW)Android passkey note:$(NC) If passkey creation still fails with"
	@echo "  'RP ID cannot be validated', connect your device and run:"
	@echo "  adb shell am compat enable DEVELOPMENT_PASSKEY_REGISTRATION $(SDK_PACKAGE)"

# =============================================================================
# Android SDK Development
# =============================================================================

SDK_PATH ?= ../siros-sdk-kotlin
SDK_PACKAGE ?= org.sirosfoundation.sdk.sample

android-setup: ## Configure local env for Android SDK testing (generates assetlinks.json, configures ADB)
	@./scripts/setup-android.sh --package $(SDK_PACKAGE)

android-config: ## Generate Android-specific VC config overlay file
	@./scripts/android-test.sh config

android-up: android-config ## Start Android overlay services without rebuilding/installing APK
	@docker network inspect e2e-test-network >/dev/null 2>&1 || docker network create e2e-test-network
	@docker compose -f docker-compose.test.yml \
		-f docker-compose.vc-services.yml \
		-f docker-compose.go-trust.yml \
		-f docker-compose.go-trust-allow.yml \
		$(if $(call _truthy,$(R2PS)),-f docker-compose.r2ps.yml) \
		-f docker-compose.android.yml \
		up -d

android-down: ## Stop Android overlay services
	@docker compose -f docker-compose.test.yml \
		-f docker-compose.vc-services.yml \
		-f docker-compose.go-trust.yml \
		-f docker-compose.go-trust-allow.yml \
		$(if $(call _truthy,$(R2PS)),-f docker-compose.r2ps.yml) \
		-f docker-compose.android.yml \
		down

android-full: ## Run full Android test flow (config + build + deploy + register + launch)
	@./scripts/android-test.sh full

android-restart: ## Restart Android test services and relaunch app
	@./scripts/android-test.sh restart

android-launch: ## Launch the installed Android sample app and print a log snapshot
	@./scripts/android-test.sh launch

android-logs: ## Follow Android sample app logs
	@./scripts/android-test.sh logs

android-test: ## Build, deploy, and test Android SDK sample app (use CMD= for subcommands: build|deploy|register|restart|logs|snapshot)
	@./scripts/android-test.sh $(CMD)
