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

.PHONY: help up down logs status status-vc \
        ensure-conformance-hosts \
        register-mocks register-vc-services clean show-branches show-images build-info pki

# =============================================================================
# Configuration
# =============================================================================

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

# Stack options (override on command line)
PDP ?= allow
VC ?=
TRANSPORT ?=
CONFORMANCE ?=

# Service URLs (published for use by sirosid-tests)
export FRONTEND_URL ?= http://localhost:3000
export BACKEND_URL ?= http://localhost:8080
export ENGINE_URL ?= http://localhost:8082
export ADMIN_URL ?= http://localhost:8081
export MOCK_VERIFIER_URL ?= http://localhost:9011
export MOCK_PDP_URL ?= http://localhost:9081
export VCTM_REGISTRY_URL ?= http://localhost:8080/registry

# VC Services URLs (external, for health checks from host)
export VC_ISSUER_URL ?= http://localhost:9000
export VC_VERIFIER_URL ?= http://localhost:9001
export VC_APIGW_URL ?= http://localhost:9003
export VC_REGISTRY_URL ?= http://localhost:9004
# VC Services URLs (internal, for container-to-container registration)
VC_APIGW_INTERNAL_URL ?= http://vc-apigw:8080
VC_VERIFIER_INTERNAL_URL ?= http://vc-verifier:8080
export GO_TRUST_ALLOW_URL ?= http://localhost:9095
export GO_TRUST_WHITELIST_URL ?= http://localhost:9096
export GO_TRUST_DENY_URL ?= http://localhost:9097

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
  ifneq ($(PDP),mock)
    COMPOSE_FILES += -f $(VC_GO_TRUST_COMPOSE)
  endif
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

# =============================================================================
# Help
# =============================================================================

help: ## Show this help
	@echo "$(GREEN)sirosid-dev$(NC) — Local Development Environment"
	@echo ""
	@echo "$(GREEN)Usage:$(NC)"
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
	@echo "$(GREEN)Examples:$(NC)"
	@echo "  make up                              # Default: frontend + backend + go-trust allow"
	@echo "  make up VC=yes                       # Add VC issuer/verifier services"
	@echo "  make up PDP=whitelist VC=1            # VC services with whitelist trust"
	@echo "  make up PDP=deny VC=1                 # Negative testing: deny all trust"
	@echo "  make up PDP=mock                      # Legacy mock PDP (no go-trust)"
	@echo "  make up TRANSPORT=wmp                 # Use WMP transport"
	@echo "  make up CONFORMANCE=yes               # Full conformance test stack"
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

# =============================================================================
# Helpers
# =============================================================================

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

up: ## Start the stack (use PDP=, VC=, TRANSPORT=, CONFORMANCE= to configure)
ifneq ($(call _truthy,$(CONFORMANCE)),)
	@$(MAKE) --no-print-directory ensure-conformance-hosts
endif
	@echo "$(GREEN)Starting sirosid-dev...$(NC)"
	@echo "  PDP:         $(_PDP_LABEL)"
	@echo "  VC services: $(_VC_LABEL)"
	@echo "  Transport:   $(_TRANSPORT_LABEL)"
	@echo "  Conformance: $(_CONFORMANCE_LABEL)"
	@echo ""
	@$(MAKE) --no-print-directory show-branches
	@$(MAKE) --no-print-directory build-info
	@echo "$(YELLOW)Building and starting containers...$(NC)"
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) \
		WALLET_NAME="$(WALLET_NAME)" \
		docker compose $(COMPOSE_FILES) up -d --build 2>&1 | \
		grep -E '^\s*(✔|=>|Building|Container|Network|Image)' || true
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
# Cleanup
# =============================================================================

clean: ## Remove all containers and volumes
	@echo "$(YELLOW)Cleaning up...$(NC)"
	-docker compose $(COMPOSE_FILES) down -v --remove-orphans
	@echo "$(GREEN)Done.$(NC)"

# =============================================================================
# PKI Generation
# =============================================================================

pki: ## Generate fresh PKI (signing keys and certificates)
	@echo "$(GREEN)Generating PKI...$(NC)"
	cd fixtures && ./create-pki.sh
