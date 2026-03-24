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
        up-ts-backend down-ts-backend \
        register-mocks clean

# =============================================================================
# Configuration
# =============================================================================

# Workspace paths - defaults assume sibling directories
FRONTEND_PATH ?= ../wallet-frontend
BACKEND_PATH ?= ../go-wallet-backend
TS_BACKEND_PATH ?= ../wallet-backend-server

# Docker compose files
PRIMARY_COMPOSE := docker-compose.test.yml
GO_TRUST_COMPOSE := docker-compose.go-trust.yml
GO_TRUST_WHITELIST_COMPOSE := docker-compose.go-trust-whitelist.yml
VC_SERVICES_COMPOSE := docker-compose.vc-services.yml
TS_BACKEND_COMPOSE := docker-compose.ts-backend.yml

# Service URLs (published for use by sirosid-tests)
export FRONTEND_URL ?= http://localhost:3000
export BACKEND_URL ?= http://localhost:8080
export ENGINE_URL ?= http://localhost:8082
export ADMIN_URL ?= http://localhost:8081
export MOCK_ISSUER_URL ?= http://localhost:9000
export MOCK_VERIFIER_URL ?= http://localhost:9001
export MOCK_PDP_URL ?= http://localhost:9091
export VCTM_REGISTRY_URL ?= http://localhost:8097
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
	@echo "  make up-ts-backend         # TypeScript backend instead of Go"
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
	@echo "  Mock Issuer:   $(MOCK_ISSUER_URL)"
	@echo "  Mock Verifier: $(MOCK_VERIFIER_URL)"
	@echo "  Trust PDP:     $(MOCK_PDP_URL)"
	@echo "  VCTM Registry: $(VCTM_REGISTRY_URL)"
	@echo ""
	@echo "$(GREEN)Integration:$(NC)"
	@echo "  Run tests with: cd ../sirosid-tests && make test"
	@echo ""

# =============================================================================
# Default Stack (Frontend + Go Backend + Mocks)
# =============================================================================

up: ## Start default development stack
	@echo "$(GREEN)Starting sirosid-dev environment...$(NC)"
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) \
		docker compose -f $(PRIMARY_COMPOSE) up -d --build
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
	-docker compose -f $(TS_BACKEND_COMPOSE) down 2>/dev/null

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
	@curl -sf $(ADMIN_URL)/health >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "wallet-admin" "✓ running" || \
		printf "  %-20s $(RED)%s$(NC)\n" "wallet-admin" "✗ not running"
	@curl -sf $(MOCK_ISSUER_URL)/.well-known/openid-credential-issuer >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "mock-issuer" "✓ running" || \
		printf "  %-20s $(RED)%s$(NC)\n" "mock-issuer" "✗ not running"
	@curl -sf $(MOCK_VERIFIER_URL)/.well-known/openid-verifier >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "mock-verifier" "✓ running" || \
		printf "  %-20s $(RED)%s$(NC)\n" "mock-verifier" "✗ not running"
	@curl -sf $(MOCK_PDP_URL)/health >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "mock-trust-pdp" "✓ running" || \
		printf "  %-20s $(RED)%s$(NC)\n" "mock-trust-pdp" "✗ not running"
	@curl -sf $(VCTM_REGISTRY_URL)/health >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "vctm-registry" "✓ running" || \
		printf "  %-20s $(RED)%s$(NC)\n" "vctm-registry" "✗ not running"
	@echo ""

# =============================================================================
# Go-Trust Stack
# =============================================================================

up-go-trust: ## Start with go-trust PDP services
	@echo "$(GREEN)Starting sirosid-dev with go-trust...$(NC)"
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) \
		docker compose -f $(PRIMARY_COMPOSE) -f $(GO_TRUST_COMPOSE) up -d --build
	@$(MAKE) --no-print-directory status

down-go-trust: ## Stop go-trust environment
	docker compose -f $(PRIMARY_COMPOSE) -f $(GO_TRUST_COMPOSE) down

up-go-trust-whitelist: ## Start with go-trust whitelist as PDP
	@echo "$(GREEN)Starting sirosid-dev with go-trust whitelist...$(NC)"
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) \
		docker compose -f $(PRIMARY_COMPOSE) -f $(GO_TRUST_COMPOSE) -f $(GO_TRUST_WHITELIST_COMPOSE) up -d --build
	@$(MAKE) --no-print-directory status

down-go-trust-whitelist: ## Stop go-trust whitelist environment
	docker compose -f $(PRIMARY_COMPOSE) -f $(GO_TRUST_COMPOSE) -f $(GO_TRUST_WHITELIST_COMPOSE) down

# =============================================================================
# VC Services Stack (Production-like)
# =============================================================================

up-vc: ## Start with production-like VC services
	@echo "$(GREEN)Starting sirosid-dev with VC services...$(NC)"
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) \
		docker compose -f $(PRIMARY_COMPOSE) -f $(VC_SERVICES_COMPOSE) up -d --build
	@$(MAKE) --no-print-directory status

down-vc: ## Stop VC services environment
	docker compose -f $(PRIMARY_COMPOSE) -f $(VC_SERVICES_COMPOSE) down

# =============================================================================
# TypeScript Backend Stack
# =============================================================================

up-ts-backend: ## Start with TypeScript wallet-backend-server
	@echo "$(GREEN)Starting sirosid-dev with TypeScript backend...$(NC)"
	FRONTEND_PATH=$(FRONTEND_PATH) TS_BACKEND_PATH=$(TS_BACKEND_PATH) \
		docker compose -f $(PRIMARY_COMPOSE) -f $(TS_BACKEND_COMPOSE) up -d --build
	@$(MAKE) --no-print-directory status

down-ts-backend: ## Stop TypeScript backend environment
	docker compose -f $(PRIMARY_COMPOSE) -f $(TS_BACKEND_COMPOSE) down

# =============================================================================
# Mock Registration
# =============================================================================

register-mocks: ## Register mock issuer/verifier with backend
	@echo "$(GREEN)Registering mock services...$(NC)"
	@curl -sf -X POST $(ADMIN_URL)/admin/issuers \
		-H "Authorization: Bearer $(ADMIN_TOKEN)" \
		-H "Content-Type: application/json" \
		-d '{"name":"Mock Issuer","url":"$(MOCK_ISSUER_URL)"}' && \
		echo "  Mock issuer registered" || \
		echo "  $(YELLOW)Warning: Could not register mock issuer$(NC)"
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
	-docker compose -f $(TS_BACKEND_COMPOSE) down -v 2>/dev/null

# =============================================================================
# PKI Generation
# =============================================================================

pki: ## Generate fresh PKI (signing keys and certificates)
	@echo "$(GREEN)Generating PKI...$(NC)"
	cd fixtures && ./create-pki.sh
