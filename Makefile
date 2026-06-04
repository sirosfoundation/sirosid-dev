# sirosid-dev Makefile
#
# Local development environment for SIROS ID wallet ecosystem.
#
# Quick Start:
#   make up       # Start default stack
#   make status   # Check service health
#   make logs     # View logs
#   make down     # Stop all services

.PHONY: help up down logs status \
        up-go-trust down-go-trust \
        up-go-trust-whitelist down-go-trust-whitelist \
        up-vc down-vc \
        up-vc-go-trust-allow up-vc-go-trust-whitelist up-vc-go-trust-deny \
        down-vc-go-trust \
        up-wmp down-wmp \
        up-conformance down-conformance \
        ensure-conformance-hosts \
        register-mocks clean show-branches show-images build-info

# =============================================================================
# Configuration
# =============================================================================

# Workspace paths - defaults assume sibling directories
FRONTEND_PATH ?= ../wallet-frontend
BACKEND_PATH ?= ../go-wallet-backend

# Docker compose files
PRIMARY_COMPOSE := docker-compose.test.yml
GO_TRUST_COMPOSE := docker-compose.go-trust.yml
GO_TRUST_WHITELIST_COMPOSE := docker-compose.go-trust-whitelist.yml
VC_SERVICES_COMPOSE := docker-compose.vc-services.yml
VC_GO_TRUST_COMPOSE := docker-compose.vc-go-trust.yml
CONFORMANCE_COMPOSE := docker-compose.conformance.yml
HTTP_TRANSPORT_COMPOSE := docker-compose.http-transport.yml
WMP_TRANSPORT_COMPOSE := docker-compose.wmp-transport.yml

# Service URLs (published for use by sirosid-tests)
export FRONTEND_URL ?= http://localhost:3000
export BACKEND_URL ?= http://localhost:8080
export ENGINE_URL ?= http://localhost:8082
export ADMIN_URL ?= http://localhost:8081
export MOCK_VERIFIER_URL ?= http://localhost:9011
export MOCK_PDP_URL ?= http://localhost:9081
export VCTM_REGISTRY_URL ?= http://localhost:8080/registry

# VC Services URLs (when running up-vc or up-vc-go-trust)
export VC_ISSUER_URL ?= http://localhost:9000
export VC_VERIFIER_URL ?= http://localhost:9001
export VC_MOCKAS_URL ?= http://localhost:9002
export VC_APIGW_URL ?= http://localhost:9003
export VC_REGISTRY_URL ?= http://localhost:9004
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
# Help
# =============================================================================

help: ## Show this help
	@echo "$(GREEN)sirosid-dev$(NC) - Local Development Environment"
	@echo ""
	@echo "$(GREEN)Environment Stacks:$(NC)"
	@echo "  make up                    # Default: frontend + Go backend + mocks"
	@echo "  make up-go-trust           # Add go-trust PDP services"
	@echo "  make up-go-trust-whitelist # Use go-trust whitelist as PDP"
	@echo "  make up-vc                 # Production-like VC services"
	@echo "  make up-wmp                # WMP transport only (JSON-RPC+SSE)"
	@echo ""
	@echo "$(GREEN)VC Services with go-trust:$(NC)"
	@echo "  make up-vc-go-trust-allow     # VC + go-trust allow-all (dev)"
	@echo "  make up-vc-go-trust-whitelist # VC + go-trust whitelist (staging)"
	@echo "  make up-vc-go-trust-deny      # VC + go-trust deny-all (test failure)"
	@echo ""
	@echo "$(GREEN)Management:$(NC)"
	@echo "  make status    # Check all service health"
	@echo "  make logs      # View Docker logs"
	@echo "  make down      # Stop all services"
	@echo ""
	@echo "$(GREEN)Service URLs (when running):$(NC)"
	@echo "  Frontend:      $(FRONTEND_URL)"
	@echo "  Backend API:   $(BACKEND_URL)"
	@echo "  Admin API:     $(ADMIN_URL)"
	@echo "  Engine:        $(ENGINE_URL)"
	@echo "  Mock Verifier: $(MOCK_VERIFIER_URL)"
	@echo "  Trust PDP:     $(MOCK_PDP_URL)"
	@echo "  VCTM Registry: $(VCTM_REGISTRY_URL)"
	@echo ""
	@echo "$(GREEN)VC Service URLs (when running up-vc*):$(NC)"
	@echo "  VC Issuer:     $(VC_ISSUER_URL)"
	@echo "  VC Verifier:   $(VC_VERIFIER_URL)"
	@echo "  VC MockAS:     $(VC_MOCKAS_URL)"
	@echo "  VC API GW:     $(VC_APIGW_URL)"
	@echo "  VC Registry:   $(VC_REGISTRY_URL)"
	@echo "  go-trust Allow:     $(GO_TRUST_ALLOW_URL)"
	@echo "  go-trust Whitelist: $(GO_TRUST_WHITELIST_URL)"
	@echo "  go-trust Deny:      $(GO_TRUST_DENY_URL)"
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
# Default Stack (Frontend + Go Backend + Mocks)
# =============================================================================

up: ## Start default development stack
	@echo "$(GREEN)Starting sirosid-dev environment...$(NC)"
	@$(MAKE) --no-print-directory show-branches
	@$(MAKE) --no-print-directory build-info
	@echo "$(YELLOW)Building and starting containers...$(NC)"
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) \
		docker compose -f $(PRIMARY_COMPOSE) up -d --build 2>&1 | \
		grep -E '^\s*(✔|=>|Building|Container|Network|Image)' || true
	@$(MAKE) --no-print-directory show-images
	@$(MAKE) --no-print-directory status
	@echo ""
	@echo "$(GREEN)Environment ready!$(NC)"
	@echo "  Frontend: $(FRONTEND_URL)"
	@echo "  Backend:  $(BACKEND_URL)"
	@echo ""
	@echo "Run tests: cd ../sirosid-tests && make test"

down: ## Stop all services
	@echo "$(YELLOW)Stopping all services...$(NC)"
	-docker compose -f $(PRIMARY_COMPOSE) down
	-docker compose -f $(GO_TRUST_COMPOSE) down 2>/dev/null
	-docker compose -f $(VC_SERVICES_COMPOSE) down 2>/dev/null
	-docker compose -f $(VC_GO_TRUST_COMPOSE) down 2>/dev/null

logs: ## View service logs
	docker compose -f $(PRIMARY_COMPOSE) logs -f

status: ## Check service health
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
	@curl -sf $(MOCK_VERIFIER_URL)/health >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "mock-verifier" "✓ running" || \
		printf "  %-20s $(RED)%s$(NC)\n" "mock-verifier" "✗ not running"
	@curl -sf $(MOCK_PDP_URL)/health >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "mock-trust-pdp" "✓ running" || \
		printf "  %-20s $(RED)%s$(NC)\n" "mock-trust-pdp" "✗ not running"
	@curl -sf $(VCTM_REGISTRY_URL)/status >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "vctm-registry" "✓ running" || \
		printf "  %-20s $(RED)%s$(NC)\n" "vctm-registry" "✗ not running"
	@echo ""

# =============================================================================
# Go-Trust Stack
# =============================================================================

up-go-trust: ## Start with go-trust PDP services
	@echo "$(GREEN)Starting sirosid-dev with go-trust...$(NC)"
	@$(MAKE) --no-print-directory show-branches
	@echo "$(YELLOW)Building and starting containers...$(NC)"
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) \
		docker compose -f $(PRIMARY_COMPOSE) -f $(GO_TRUST_COMPOSE) up -d --build 2>&1 | \
		grep -E '^\s*(✔|=>|Building|Container|Network|Image)' || true
	@$(MAKE) --no-print-directory show-images
	@$(MAKE) --no-print-directory status

down-go-trust: ## Stop go-trust environment
	docker compose -f $(PRIMARY_COMPOSE) -f $(GO_TRUST_COMPOSE) down

up-go-trust-whitelist: ## Start with go-trust whitelist as PDP
	@echo "$(GREEN)Starting sirosid-dev with go-trust whitelist...$(NC)"
	@$(MAKE) --no-print-directory show-branches
	@echo "$(YELLOW)Building and starting containers...$(NC)"
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) \
		docker compose -f $(PRIMARY_COMPOSE) -f $(GO_TRUST_COMPOSE) -f $(GO_TRUST_WHITELIST_COMPOSE) up -d --build 2>&1 | \
		grep -E '^\s*(✔|=>|Building|Container|Network|Image)' || true
	@$(MAKE) --no-print-directory show-images
	@$(MAKE) --no-print-directory status

down-go-trust-whitelist: ## Stop go-trust whitelist environment
	docker compose -f $(PRIMARY_COMPOSE) -f $(GO_TRUST_COMPOSE) -f $(GO_TRUST_WHITELIST_COMPOSE) down

# =============================================================================
# VC Services Stack (Production-like)
# =============================================================================

up-vc: ## Start with production-like VC services
	@echo "$(GREEN)Starting sirosid-dev with VC services...$(NC)"
	@$(MAKE) --no-print-directory show-branches
	@echo "$(YELLOW)Building and starting containers...$(NC)"
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) \
		docker compose -f $(PRIMARY_COMPOSE) -f $(VC_SERVICES_COMPOSE) up -d --build 2>&1 | \
		grep -E '^\s*(✔|=>|Building|Container|Network|Image)' || true
	@$(MAKE) --no-print-directory show-images
	@$(MAKE) --no-print-directory status

down-vc: ## Stop VC services environment
	docker compose -f $(PRIMARY_COMPOSE) -f $(VC_SERVICES_COMPOSE) down

# =============================================================================
# VC Services + go-trust Stack
# =============================================================================

up-vc-go-trust-allow: ## Start VC services with go-trust allow-all PDP
	@echo "$(GREEN)Starting sirosid-dev with VC services + go-trust (allow-all)...$(NC)"
	@echo "  go-trust mode: ALLOW ALL (development)"
	@$(MAKE) --no-print-directory show-branches
	@echo "$(YELLOW)Building and starting containers...$(NC)"
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) \
		GO_TRUST_MODE=allow \
		docker compose -f $(PRIMARY_COMPOSE) -f $(VC_SERVICES_COMPOSE) -f $(VC_GO_TRUST_COMPOSE) up -d --build wallet-backend wallet-frontend go-trust-allow vc-issuer vc-verifier vc-apigw vc-registry vc-mockas mongodb 2>&1 | \
		grep -E '^\s*(✔|=>|Building|Container|Network|Image)' || true
	@$(MAKE) --no-print-directory show-images
	@$(MAKE) --no-print-directory status-vc
	@echo ""
	@echo "$(GREEN)go-trust PDP running at $(GO_TRUST_ALLOW_URL)$(NC)"

up-vc-go-trust-whitelist: ## Start VC services with go-trust whitelist PDP
	@echo "$(GREEN)Starting sirosid-dev with VC services + go-trust (whitelist)...$(NC)"
	@echo "  go-trust mode: WHITELIST (staging)"
	@$(MAKE) --no-print-directory show-branches
	@echo "$(YELLOW)Building and starting containers...$(NC)"
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) \
		GO_TRUST_MODE=whitelist \
		docker compose -f $(PRIMARY_COMPOSE) -f $(VC_SERVICES_COMPOSE) -f $(VC_GO_TRUST_COMPOSE) up -d --build wallet-backend wallet-frontend go-trust-whitelist vc-issuer vc-verifier vc-apigw vc-registry vc-mockas mongodb 2>&1 | \
		grep -E '^\s*(✔|=>|Building|Container|Network|Image)' || true
	@$(MAKE) --no-print-directory show-images
	@$(MAKE) --no-print-directory status-vc
	@echo ""
	@echo "$(GREEN)go-trust PDP running at $(GO_TRUST_WHITELIST_URL)$(NC)"

up-vc-go-trust-deny: ## Start VC services with go-trust deny-all PDP (negative testing)
	@echo "$(GREEN)Starting sirosid-dev with VC services + go-trust (deny-all)...$(NC)"
	@echo "  go-trust mode: DENY ALL (negative testing)"
	@$(MAKE) --no-print-directory show-branches
	@echo "$(YELLOW)Building and starting containers...$(NC)"
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) \
		GO_TRUST_MODE=deny \
		docker compose -f $(PRIMARY_COMPOSE) -f $(VC_SERVICES_COMPOSE) -f $(VC_GO_TRUST_COMPOSE) up -d --build wallet-backend wallet-frontend go-trust-deny vc-issuer vc-verifier vc-apigw vc-registry vc-mockas mongodb 2>&1 | \
		grep -E '^\s*(✔|=>|Building|Container|Network|Image)' || true
	@$(MAKE) --no-print-directory show-images
	@$(MAKE) --no-print-directory status-vc
	@echo ""
	@echo "$(GREEN)go-trust PDP running at $(GO_TRUST_DENY_URL)$(NC)"

down-vc-go-trust: ## Stop VC + go-trust environment
	docker compose -f $(PRIMARY_COMPOSE) -f $(VC_SERVICES_COMPOSE) -f $(VC_GO_TRUST_COMPOSE) down

# =============================================================================
# OpenID Conformance Suite
# =============================================================================

CONFORMANCE_HOSTNAME := localhost.emobix.co.uk

ensure-conformance-hosts: ## Ensure /etc/hosts has the conformance suite entry
	@if grep -q '$(CONFORMANCE_HOSTNAME)' /etc/hosts; then \
		echo "$(GREEN)✓ /etc/hosts already has $(CONFORMANCE_HOSTNAME)$(NC)"; \
	else \
		echo "$(YELLOW)Adding 127.0.0.1 $(CONFORMANCE_HOSTNAME) to /etc/hosts (requires sudo)...$(NC)"; \
		echo '127.0.0.1 $(CONFORMANCE_HOSTNAME)' | sudo tee -a /etc/hosts >/dev/null; \
		echo "$(GREEN)✓ Added $(CONFORMANCE_HOSTNAME) to /etc/hosts$(NC)"; \
	fi

up-conformance: ensure-conformance-hosts ## Start wallet + go-trust allow-all + conformance suite
	@echo "$(GREEN)Starting sirosid-dev with conformance suite...$(NC)"
	@echo "  Conformance URL: https://$(CONFORMANCE_HOSTNAME):8443/"
	@echo "  go-trust mode: ALLOW ALL"
	@echo "  Transport: HTTP proxy"
	@$(MAKE) --no-print-directory show-branches
	@echo "$(YELLOW)Building and starting containers...$(NC)"
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) \
		GO_TRUST_MODE=allow \
		docker compose \
			-f $(PRIMARY_COMPOSE) \
			-f $(VC_SERVICES_COMPOSE) \
			-f $(VC_GO_TRUST_COMPOSE) \
			-f $(CONFORMANCE_COMPOSE) \
			-f $(HTTP_TRANSPORT_COMPOSE) \
			up -d --build 2>&1 | \
		grep -E '^\s*(✔|=>|Building|Container|Network|Image)' || true
	@$(MAKE) --no-print-directory show-images
	@echo ""
	@echo "$(GREEN)Waiting for conformance suite to start (this may take 60s+)...$(NC)"
	@for i in $$(seq 1 30); do \
		curl -fsk https://localhost.emobix.co.uk:8443/api/runner/available >/dev/null 2>&1 && break; \
		sleep 5; \
	done
	@curl -fsk https://localhost.emobix.co.uk:8443/api/runner/available >/dev/null 2>&1 && \
		echo "$(GREEN)✓ Conformance suite ready$(NC)" || \
		echo "$(YELLOW)○ Conformance suite still starting... check: docker logs conformance-suite-server$(NC)"
	@$(MAKE) --no-print-directory status-vc

down-conformance: ## Stop conformance suite environment
	docker compose \
		-f $(PRIMARY_COMPOSE) \
		-f $(VC_SERVICES_COMPOSE) \
		-f $(VC_GO_TRUST_COMPOSE) \
		-f $(CONFORMANCE_COMPOSE) \
		-f $(HTTP_TRANSPORT_COMPOSE) \
		down

status-vc: ## Check VC service health
	@echo "$(GREEN)VC Service Status:$(NC)"
	@echo ""
	@printf "  %-20s %s\n" "Service" "Status"
	@printf "  %-20s %s\n" "-------" "------"
	@curl -sf $(VC_ISSUER_URL)/.well-known/openid-credential-issuer >/dev/null 2>&1 && \
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
	@curl -sf $(VC_MOCKAS_URL)/ >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "vc-mockas" "✓ running" || \
		printf "  %-20s $(RED)%s$(NC)\n" "vc-mockas" "✗ not running"
	@curl -sf $(GO_TRUST_ALLOW_URL)/health >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "go-trust-allow" "✓ running" || \
		printf "  %-20s $(YELLOW)%s$(NC)\n" "go-trust-allow" "- not started"
	@curl -sf $(GO_TRUST_WHITELIST_URL)/health >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "go-trust-whitelist" "✓ running" || \
		printf "  %-20s $(YELLOW)%s$(NC)\n" "go-trust-whitelist" "- not started"
	@curl -sf $(GO_TRUST_DENY_URL)/health >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "go-trust-deny" "✓ running" || \
		printf "  %-20s $(YELLOW)%s$(NC)\n" "go-trust-deny" "- not started"
	@echo ""

# =============================================================================
# WMP Transport Stack
# =============================================================================

up-wmp: ## Start with WMP transport only
	@echo "$(GREEN)Starting sirosid-dev with WMP transport...$(NC)"
	@$(MAKE) --no-print-directory show-branches
	@echo "$(YELLOW)Building and starting containers...$(NC)"
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) \
		docker compose -f $(PRIMARY_COMPOSE) -f $(WMP_TRANSPORT_COMPOSE) up -d --build 2>&1 | \
		grep -E '^\s*(✔|=>|Building|Container|Network|Image)' || true
	@$(MAKE) --no-print-directory show-images
	@$(MAKE) --no-print-directory status

down-wmp: ## Stop WMP transport environment
	docker compose -f $(PRIMARY_COMPOSE) -f $(WMP_TRANSPORT_COMPOSE) down

# =============================================================================
# Mock Registration
# =============================================================================

register-mocks: ## Register mock verifier with backend
	@echo "$(GREEN)Registering mock services...$(NC)"
	@curl -sf -X POST $(ADMIN_URL)/admin/verifiers \
		-H "Authorization: Bearer $(ADMIN_TOKEN)" \
		-H "Content-Type: application/json" \
		-d '{"name":"Mock Verifier","url":"$(MOCK_VERIFIER_URL)"}' && \
		echo "  Mock verifier registered" || \
		echo "  $(YELLOW)Warning: Could not register mock verifier$(NC)"

# =============================================================================
# Cleanup
# =============================================================================

clean: ## Remove all containers and volumes
	@echo "$(YELLOW)Cleaning up...$(NC)"
	-docker compose -f $(PRIMARY_COMPOSE) down -v --remove-orphans
	-docker compose -f $(GO_TRUST_COMPOSE) down -v 2>/dev/null
	-docker compose -f $(VC_SERVICES_COMPOSE) down -v 2>/dev/null
	-docker compose -f $(VC_GO_TRUST_COMPOSE) down -v 2>/dev/null
	-docker compose -f $(CONFORMANCE_COMPOSE) down -v 2>/dev/null

# =============================================================================
# PKI Generation
# =============================================================================

pki: ## Generate fresh PKI (signing keys and certificates)
	@echo "$(GREEN)Generating PKI...$(NC)"
	cd fixtures && ./create-pki.sh
