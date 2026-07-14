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
	usb-android-setup usb-android-config usb-android-up usb-android-down usb-android-full usb-android-restart usb-android-launch usb-android-logs usb-android-status usb-android-test \
	usb-android-test-wsca \
	usb-android-conformance publish-conformance-results \
	install tunnel tunnel-stop tunnel-status restart-with-tunnels ensure-tunnels

# =============================================================================
# Configuration
# =============================================================================

# GitHub org
GITHUB_ORG ?= git@github.com:sirosfoundation

# Workspace paths - defaults assume sibling directories
FRONTEND_PATH ?= ../wallet-frontend
BACKEND_PATH ?= ../go-wallet-backend
FACETEC_PATH ?= ../facetec-api

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
TUNNEL_COMPOSE := docker-compose.tunnel.yml
TUNNEL_VC_COMPOSE := docker-compose.tunnel-vc.yml
FACETEC_COMPOSE := docker-compose.facetec.yml
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
TUNNELS ?=
GOLDEN ?=
REBUILD ?=
FACETEC ?=

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
export FACETEC_API_URL ?= http://$(_HOST):8085

# R2PS service URLs
export R2PS_URL ?= http://$(_HOST):8443
export R2PS_ADMIN_URL ?= http://$(_HOST):8444
export R2PS_ADMIN_DEV_TOKEN ?= r2ps-e2e-dev-token-for-testing-only

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

# facetec-api (FaceTec SDK <-> vc issuer bridge). Requires VC services for
# credential issuance via vc-apigw, so it implies VC=yes.
ifneq ($(call _truthy,$(FACETEC)),)
  ifeq ($(findstring $(VC_SERVICES_COMPOSE),$(COMPOSE_FILES)),)
    COMPOSE_FILES += -f $(VC_SERVICES_COMPOSE)
    _VC_LABEL := yes (via facetec)
  endif
  COMPOSE_FILES += -f $(FACETEC_COMPOSE)
  _FACETEC_LABEL := yes
else
  _FACETEC_LABEL := no
endif

# Transport override
ifeq ($(TRANSPORT),wmp)
  COMPOSE_FILES += -f $(WMP_TRANSPORT_COMPOSE)
  _TRANSPORT_LABEL := WMP (JSON-RPC+SSE)
else ifeq ($(TRANSPORT),http)
  COMPOSE_FILES += -f $(HTTP_TRANSPORT_COMPOSE)
  _TRANSPORT_LABEL := HTTP proxy (deprecated)
else
  _TRANSPORT_LABEL := WebSocket (default)
endif

# Conformance suite (implies VC + allow)
ifneq ($(call _truthy,$(CONFORMANCE)),)
  # Ensure required overlays are present
  ifeq ($(findstring $(VC_SERVICES_COMPOSE),$(COMPOSE_FILES)),)
    COMPOSE_FILES += -f $(VC_SERVICES_COMPOSE)
  endif
  ifeq ($(findstring $(VC_GO_TRUST_COMPOSE),$(COMPOSE_FILES)),)
    COMPOSE_FILES += -f $(VC_GO_TRUST_COMPOSE)
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

# Cloudflare quick tunnels (host-managed, not container-managed)
ifneq ($(call _truthy,$(TUNNELS)),)
	COMPOSE_FILES += -f $(TUNNEL_COMPOSE)
  ifneq ($(call _truthy,$(VC)),)
	COMPOSE_FILES += -f $(TUNNEL_VC_COMPOSE)
  endif
	_TUNNELS_LABEL := yes
	# When VC services are also in the stack, vc-apigw/vc-issuer need a tunnel-patched
	# config too — the base fixtures/vc-config.yaml hardcodes "https://vc-proxy:8443"
	# (only reachable when the separate conformance overlay is active), which would
	# otherwise end up embedded as an unreachable credential_issuer in every credential
	# offer. See scripts/generate-tunnel-config.py and the config-regeneration step below.
	ifneq ($(findstring $(VC_SERVICES_COMPOSE),$(COMPOSE_FILES)),)
		COMPOSE_FILES += -f $(TUNNEL_VC_COMPOSE)
		_TUNNEL_VC_LABEL := yes
	else
		_TUNNEL_VC_LABEL := no
	endif
else
	_TUNNELS_LABEL := no
	_TUNNEL_VC_LABEL := no
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
	@echo "$(GREEN)Primary Targets:$(NC)"
	@echo "  make setup                           Clone sibling repos"
	@echo "  make install                         Show dependency/install notes"
	@echo "  make up [STACK OPTIONS]              Start the stack with selected overlays"
	@echo "  make down                            Stop stack containers"
	@echo "  make status                          Check core service health"
	@echo "  make logs                            View Docker logs"
	@echo "  make clean                           Remove containers, volumes, build cache"
	@echo ""
	@echo "$(GREEN)Tunnel Targets:$(NC)"
	@echo "  make tunnel-status                   Show active tunnel URLs and host processes"
	@echo "  make tunnel-stop                     Stop Cloudflare tunnel processes and remove .env.tunnel"
	@echo "  make restart-with-tunnels            Deprecated alias for: make up TUNNELS=yes"
	@echo ""
	@echo "$(GREEN)Android Targets:$(NC)"
	@echo "  make android-setup [APP_PACKAGE=...] Generate assetlinks.json + .env.android and try ADB setup"
	@echo "  make android-config                  Generate Android-specific VC config overlay"
	@echo "  make android-up [SDK_REBUILD=yes]    Start Android overlay services (SDK_REBUILD=yes rebuilds Rust + SDK)"
	@echo "  make android-down                    Stop Android overlay services"
	@echo "  make android-full                    Full Android flow (config + build + deploy + launch)"
	@echo "  make android-restart                 Restart Android test services + relaunch app"
	@echo "  make android-launch                  Launch installed sample app + log snapshot"
	@echo "  make android-logs                    Follow Android app logs"
	@echo ""
	@echo "$(GREEN)USB Android Targets (physical device):$(NC)"
	@echo "  make usb-android-setup               Set up port forwarding + assetlinks + config"
	@echo "  make usb-android-config              Generate USB-specific VC config (localhost via adb reverse)"
	@echo "  make usb-android-up [SDK_REBUILD=yes] Start USB Android overlay services"
	@echo "  make usb-android-down                Stop USB Android overlay + remove port forwarding"
	@echo "  make usb-android-full                Full USB flow (setup + build + deploy + launch)"
	@echo "  make usb-android-restart             Restart USB Android test services + relaunch app"
	@echo "  make usb-android-launch              Launch installed sample app on USB device"
	@echo "  make usb-android-logs                Follow Android app logs from USB device"
	@echo "  make usb-android-status              Show device info, port forwarding, app status"
	@echo ""
	@echo "$(GREEN)USB Android WSCA Tests (physical device):$(NC)"
	@echo "  make usb-android-test-wsca           Run WSCA lifecycle conformance tests"
	@echo "    R2PS_URL=http://...                include R2PS plugin (default: auto from R2PS stack)"
	@echo "    FIDO2_ENABLED=true                 include FIDO2/YubiKey plugin"
	@echo ""
	@echo "$(GREEN)Stack Options:$(NC)  (pass on the make command line to 'make up')"
	@echo ""
	@echo "  $(YELLOW)PDP=$(NC)<allow|whitelist|deny|mock>"
	@echo "                     Select trust policy provider"
	@echo "                     default: $(GREEN)allow$(NC)"
	@echo ""
	@echo "  $(YELLOW)VC=$(NC)<yes|no>"
	@echo "                     Enable production-like VC services"
	@echo "                     Adds issuer, verifier, apigw, registry, mongodb"
	@echo ""
	@echo "  $(YELLOW)TRANSPORT=$(NC)<websocket|wmp>"
	@echo "                     Select wallet transport mode"
	@echo "                     default: websocket (http is deprecated)"
	@echo ""
	@echo "  $(YELLOW)CONFORMANCE=$(NC)<yes|no>"
	@echo "                     Enable OpenID conformance suite"
	@echo "                     Implies: VC=yes PDP=allow"
	@echo ""
	@echo "  $(YELLOW)R2PS=$(NC)<yes|no>"
	@echo "                     Enable R2PS service + SoftHSM2"
	@echo ""
	@echo "  $(YELLOW)DOMAIN=$(NC)<hostname>"
	@echo "                     Replace localhost URLs with a local-network hostname"
	@echo ""
	@echo "  $(YELLOW)TUNNELS=$(NC)<yes|no>"
	@echo "                     Enable Cloudflare quick tunnel overlay"
	@echo "                     Auto-creates or reuses .env.tunnel via cloudflared"
	@echo ""
	@echo "  $(YELLOW)GOLDEN=$(NC)<yes|release-name>"
	@echo "                     Use pre-built images from golden releases"
	@echo ""
	@echo "  $(YELLOW)FACETEC=$(NC)<yes|no>"
	@echo "                     Enable facetec-api (FaceTec SDK <-> vc issuer bridge)"
	@echo "                     Implies VC=yes. Requires FACETEC_SERVER_URL to be exported"
	@echo "                     in your shell (a live credential — never committed here)."
	@echo ""
	@echo "  $(YELLOW)REBUILD=$(NC)<yes|no>"
	@echo "                     Force no-cache image rebuild before startup"
	@echo ""
	@echo "$(GREEN)Interaction Rules:$(NC)"
	@echo "  - $(YELLOW)TUNNELS=yes$(NC) and $(YELLOW)DOMAIN=...$(NC) are mutually exclusive"
	@echo "  - $(YELLOW)CONFORMANCE=yes$(NC) automatically enables VC services with allow-all trust"
	@echo "  - $(YELLOW)make down$(NC) stops containers but does not stop Cloudflare tunnel processes"
	@echo "  - $(YELLOW)make tunnel-stop$(NC) is required to remove active tunnel URLs"
	@echo "  - $(YELLOW)APP_PACKAGE=...$(NC) affects android-setup only; it is not a stack-wide option"
	@echo ""
	@echo "$(GREEN)Useful Examples:$(NC)"
	@echo "  make up"
	@echo "  make up VC=yes"
	@echo "  make up PDP=whitelist VC=yes"
	@echo "  make up CONFORMANCE=yes"
	@echo "  make up R2PS=yes VC=yes"
	@echo "  make up DOMAIN=myhost.local VC=yes"
	@echo "  make up TUNNELS=yes VC=yes"
	@echo "  make up GOLDEN=beta_r2 VC=yes"
	@echo "  FACETEC_SERVER_URL=https://user:pass@ft.example.org make up FACETEC=yes"
	@echo "  make android-setup APP_PACKAGE=com.example.app"
	@echo ""
	@echo "$(GREEN)Current Default URLs:$(NC)"
	@echo "  Frontend:      $(FRONTEND_URL)"
	@echo "  Backend API:   $(BACKEND_URL)"
	@echo "  Admin API:     $(ADMIN_URL)"
	@echo "  Engine:        $(ENGINE_URL)"
	@echo "  facetec-api:   $(FACETEC_API_URL)  (FACETEC=yes only)"
	@echo ""
	@echo "$(GREEN)Source Path Overrides:$(NC)"
	@echo ""
	@echo "  $(YELLOW)FRONTEND_PATH=$(NC)   wallet-frontend source  (default: $(GREEN)../wallet-frontend$(NC))"
	@echo "  $(YELLOW)BACKEND_PATH=$(NC)    go-wallet-backend source (default: $(GREEN)../go-wallet-backend$(NC))"
	@echo "  $(YELLOW)VC_PATH=$(NC)          vc services source     (default: $(GREEN)../vc$(NC))"
	@echo "  $(YELLOW)GO_TRUST_PATH=$(NC)    go-trust source        (default: $(GREEN)../go-trust$(NC))"
	@echo "  $(YELLOW)FACETEC_PATH=$(NC)     facetec-api source     (default: $(GREEN)../facetec-api$(NC))"
	@echo ""
	@echo "$(GREEN)Other Variables:$(NC)"
	@echo ""
	@echo "  $(YELLOW)WALLET_NAME=$(NC)      Wallet display name (default: $(GREEN)SIROS ID (dev)$(NC))"
	@echo "  $(YELLOW)APP_PACKAGE=$(NC)     Used by make android-setup (default: $(GREEN)org.sirosfoundation.sdk.sample$(NC))"
	@echo ""
	@echo "$(GREEN)Integration:$(NC)"
	@echo "  Run tests with: cd ../sirosid-tests && make test"
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
	@if [ -d "$(FACETEC_PATH)/.git" ]; then \
		printf "  %-24s %s\n" "facetec-api:" "$$(git -C $(FACETEC_PATH) branch --show-current)"; \
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
		"facetec-api:$(FACETEC_PATH)" \
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
ifneq ($(call _truthy,$(TUNNELS)),)
	@if [ -n "$(DOMAIN)" ]; then \
		echo "$(RED)Error: TUNNELS=yes cannot be combined with DOMAIN=$(DOMAIN).$(NC)"; \
		echo "  Use either Cloudflare quick tunnels or a local custom domain, not both."; \
		exit 1; \
	fi
	@$(MAKE) --no-print-directory ensure-tunnels
ifneq ($(findstring $(VC_SERVICES_COMPOSE),$(COMPOSE_FILES)),)
	@# Regenerate the tunnel-patched vc-config now that .env.tunnel is populated
	@# (either just now, or reused from an already-running tunnel session).
	@. ./.env.tunnel && python3 scripts/generate-tunnel-config.py \
		--apigw-url "$$TUNNEL_VC_APIGW_URL" \
		--frontend-url "$$TUNNEL_FRONTEND_URL"
endif
endif
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
	@# Pre-flight: (re)build the gobuild base image from local ../vc source.
	@# The docker.sunet.se/iam_vc/gobuild:local image on the registry can lag
	@# behind ../vc's go.mod (observed: registry image had go1.26.1 while
	@# go.mod required go1.26.4), and dockerfiles/worker sets GOPROXY=off, so
	@# the mismatch fails as "toolchain not available" instead of downloading
	@# the right Go version. Building locally guarantees a matching toolchain
	@# on any architecture.
	@_VC_DIR="$${VC_PATH:-../vc}"; \
		echo "$(YELLOW)Building gobuild base image (ensures Go toolchain matches ../vc/go.mod)...$(NC)"; \
		docker build --quiet --tag docker.sunet.se/iam_vc/gobuild:local \
			--file "$$_VC_DIR/dockerfiles/gobuild" "$$_VC_DIR" >/dev/null
endif
ifneq ($(call _truthy,$(FACETEC)),)
	@# Pre-flight: ../facetec-api must exist for the facetec-api build
	@if [ ! -d "$(FACETEC_PATH)" ]; then \
		echo "$(RED)Error: FACETEC=yes requires the 'facetec-api' repo at $(FACETEC_PATH)$(NC)"; \
		echo "  Run: make setup   (clones all required sibling repos)"; \
		echo "  Or:  git clone $(GITHUB_ORG)/facetec-api.git $(FACETEC_PATH)"; \
		exit 1; \
	fi
	@# Pre-flight: FACETEC_SERVER_URL is a live credential with no safe default;
	@# fail fast with a clear message rather than letting the container crash-loop.
	@if [ -z "$$FACETEC_SERVER_URL" ]; then \
		echo "$(RED)Error: FACETEC=yes requires FACETEC_SERVER_URL to be set.$(NC)"; \
		echo "  export FACETEC_SERVER_URL=\"https://user:pass@your-facetec-server.example.org\""; \
		exit 1; \
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
	@echo "  Tunnels:     $(_TUNNELS_LABEL)"
	@echo "  facetec-api: $(_FACETEC_LABEL)"
ifneq ($(call _truthy,$(TUNNELS)),)
	@echo "  vc-config:   $(_TUNNEL_VC_LABEL) (tunnel-patched credential_issuer/token_endpoint/CORS)"
endif
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
		{ [ -f .env.tunnel ] && . ./.env.tunnel && export TUNNEL_FRONTEND_URL TUNNEL_BACKEND_URL TUNNEL_ENGINE_URL TUNNEL_RPID TUNNEL_VC_VERIFIER_URL TUNNEL_VC_APIGW_URL || true; } && \
		{ [ -f .env.android ] && . ./.env.android && export APK_KEY_HASH || true; } && \
	WALLET_NAME="$(WALLET_NAME)" \
		docker compose $(COMPOSE_FILES) up -d --pull always 2>&1 | \
		grep -E '^\s*(✔|=>|Pulling|Container|Network|Image)' || true
else
ifneq ($(call _truthy,$(REBUILD)),)
	@echo "$(YELLOW)Force-rebuilding all images (no cache)...$(NC)"
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) FACETEC_PATH=$(FACETEC_PATH) \
		WALLET_NAME="$(WALLET_NAME)" \
		docker compose $(COMPOSE_FILES) build --no-cache 2>&1 | \
		grep -E '^\s*(✔|=>|Building|Container|Network|Image)' || true
endif
	@_LOG=$$(mktemp /tmp/compose.XXXXXX); \
	[ -f .env.tunnel ] && . ./.env.tunnel && export TUNNEL_FRONTEND_URL TUNNEL_BACKEND_URL TUNNEL_ENGINE_URL TUNNEL_RPID TUNNEL_VC_VERIFIER_URL TUNNEL_VC_APIGW_URL || true; \
	[ -f .env.android ] && . ./.env.android && export APK_KEY_HASH || true; \
	FRONTEND_PATH=$(FRONTEND_PATH) BACKEND_PATH=$(BACKEND_PATH) FACETEC_PATH=$(FACETEC_PATH) \
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
	@if [ "$(call _truthy,$(TUNNELS))" != "" ] && [ -f .env.tunnel ]; then \
		. ./.env.tunnel; \
		echo ""; \
		echo "$(GREEN)Tunnel URLs:$(NC)"; \
		echo "  Frontend:    $$TUNNEL_FRONTEND_URL"; \
		echo "  Backend:     $$TUNNEL_BACKEND_URL"; \
		echo "  Engine:      $$TUNNEL_ENGINE_URL"; \
		if [ -n "$${TUNNEL_VC_VERIFIER_URL:-}" ]; then \
			echo "  VC Verifier: $$TUNNEL_VC_VERIFIER_URL"; \
		fi; \
		echo "  facetec-api: $$TUNNEL_FACETEC_API_URL"; \
		echo "  vc-apigw:    $$TUNNEL_VC_APIGW_URL"; \
	fi
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
	@if [ "$(call _truthy,$(TUNNELS))" != "" ]; then \
		echo ""; \
		echo "$(GREEN)Refreshing Android passkey setup for tunnel usage...$(NC)"; \
		$(MAKE) --no-print-directory android-setup APP_PACKAGE=$(APP_PACKAGE) 2>/dev/null || true; \
		echo ""; \
		echo "$(YELLOW)Tunnel note:$(NC) tunnel processes are host-side and stay running after 'make down'."; \
		echo "  Use 'make tunnel-stop' when you want to tear them down."; \
	fi
	@echo ""
	@echo "$(GREEN)Environment ready!$(NC)"
	@echo "  Frontend: $(FRONTEND_URL)"
	@echo "  Backend:  $(BACKEND_URL)"
ifneq ($(call _truthy,$(FACETEC)),)
	@echo "  facetec-api: $(FACETEC_API_URL)"
endif
	@echo ""

down: ## Stop all services
	@echo "$(YELLOW)Stopping all services...$(NC)"
	-@{ [ -f .env.tunnel ] && . ./.env.tunnel && export TUNNEL_FRONTEND_URL TUNNEL_BACKEND_URL TUNNEL_ENGINE_URL TUNNEL_RPID TUNNEL_VC_VERIFIER_URL TUNNEL_VC_APIGW_URL || true; \
		[ -f .env.android ] && . ./.env.android && export APK_KEY_HASH || true; \
		docker compose $(COMPOSE_FILES) down; }
	@echo "$(GREEN)Done.$(NC)"

logs: ## View service logs
	@{ [ -f .env.tunnel ] && . ./.env.tunnel && export TUNNEL_FRONTEND_URL TUNNEL_BACKEND_URL TUNNEL_ENGINE_URL TUNNEL_RPID TUNNEL_VC_VERIFIER_URL TUNNEL_VC_APIGW_URL || true; \
		[ -f .env.android ] && . ./.env.android && export APK_KEY_HASH || true; \
		docker compose $(COMPOSE_FILES) logs -f; }

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
	@curl -sf $(FACETEC_API_URL)/livez >/dev/null 2>&1 && \
		printf "  %-20s $(GREEN)%s$(NC)\n" "facetec-api" "✓ running" || true
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
	@_VC_APIGW_REG_URL="$(VC_APIGW_INTERNAL_URL)"; \
	_VC_VERIFIER_REG_URL="$(VC_VERIFIER_INTERNAL_URL)"; \
	if [ -f .env.tunnel ]; then \
		. ./.env.tunnel; \
		if [ -n "$${TUNNEL_VC_APIGW_URL:-}" ]; then _VC_APIGW_REG_URL="$$TUNNEL_VC_APIGW_URL"; fi; \
		if [ -n "$${TUNNEL_VC_VERIFIER_URL:-}" ]; then _VC_VERIFIER_REG_URL="$$TUNNEL_VC_VERIFIER_URL"; fi; \
	fi; \
	curl -sf -X POST $(ADMIN_URL)/admin/tenants/$(TENANT_ID)/issuers \
		-H "Authorization: Bearer $(ADMIN_TOKEN)" \
		-H "Content-Type: application/json" \
		-d "{\"credential_issuer_identifier\":\"$$_VC_APIGW_REG_URL\",\"visible\":true}" && \
		echo "  $(GREEN)✓ VC issuer registered ($$_VC_APIGW_REG_URL)$(NC)" || \
		echo "  $(YELLOW)Warning: Could not register VC issuer$(NC)"; \
	curl -sf -X POST $(ADMIN_URL)/admin/tenants/$(TENANT_ID)/verifiers \
		-H "Authorization: Bearer $(ADMIN_TOKEN)" \
		-H "Content-Type: application/json" \
		-d "{\"name\":\"VC Verifier\",\"url\":\"$$_VC_VERIFIER_REG_URL\"}" && \
		echo "  $(GREEN)✓ VC verifier registered ($$_VC_VERIFIER_REG_URL)$(NC)" || \
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
	-@{ [ -f .env.tunnel ] && . ./.env.tunnel && export TUNNEL_FRONTEND_URL TUNNEL_BACKEND_URL TUNNEL_ENGINE_URL TUNNEL_RPID TUNNEL_VC_VERIFIER_URL TUNNEL_VC_APIGW_URL || true; \
		[ -f .env.android ] && . ./.env.android && export APK_KEY_HASH || true; \
		docker compose $(COMPOSE_FILES) down -v --remove-orphans; }
	-@{ [ -f .env.tunnel ] && . ./.env.tunnel && export TUNNEL_FRONTEND_URL TUNNEL_BACKEND_URL TUNNEL_ENGINE_URL TUNNEL_RPID TUNNEL_VC_VERIFIER_URL TUNNEL_VC_APIGW_URL || true; \
		[ -f .env.android ] && . ./.env.android && export APK_KEY_HASH || true; \
		docker compose $(COMPOSE_FILES) down -v --remove-orphans 2>/dev/null; }
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
	vc:main \
	facetec-api:main

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

install: ## Install all project dependencies
	@echo "$(GREEN)No npm dependencies required.$(NC)"
	@echo "$(GREEN)run-android-conformance.mjs uses only built-in Node.js modules.$(NC)"

# =============================================================================
# R2PS Service
# =============================================================================

r2ps-setup: ## Verify R2PS service health and show status
	@./scripts/setup-r2ps.sh

# =============================================================================
# Cloudflare Tunnels (on-demand TLS domains for mobile/external testing)
# =============================================================================

ensure-tunnels: ## Ensure Cloudflare quick tunnels exist for TUNNELS=yes
	@./scripts/tunnel.sh ensure

tunnel: ## Start Cloudflare quick tunnels (no account needed) and show URLs
	@./scripts/tunnel.sh start

tunnel-stop: ## Stop Cloudflare tunnels and clean up
	@./scripts/tunnel.sh stop

tunnel-status: ## Show active tunnel URLs and process status
	@./scripts/tunnel.sh status

restart-with-tunnels: ## Restart the stack using Cloudflare tunnel URLs
	@echo "$(YELLOW)restart-with-tunnels is deprecated.$(NC)"
	@echo "  Use: make up TUNNELS=yes [your other options]"
	@$(MAKE) --no-print-directory up TUNNELS=yes

# =============================================================================
# Android SDK Development
# =============================================================================

SDK_PATH ?= ../siros-sdk-kotlin
APP_PACKAGE ?= org.sirosfoundation.sdk.sample

android-setup: ## Configure local env for Android SDK testing (generates assetlinks.json, configures ADB)
	@./scripts/setup-android.sh --package $(APP_PACKAGE)

android-config: ## Generate Android-specific VC config overlay file
	@./scripts/android-test.sh config

android-up: android-config ## Start Android overlay services (SDK_REBUILD=yes to rebuild Rust crates + SDK first)
	@docker network inspect e2e-test-network >/dev/null 2>&1 || docker network create e2e-test-network
	$(if $(call _truthy,$(SDK_REBUILD)),@./scripts/android-test.sh rebuild)
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
	@SDK_REBUILD=$(SDK_REBUILD) ./scripts/android-test.sh full

android-restart: ## Restart Android test services and relaunch app
	@./scripts/android-test.sh restart

android-launch: ## Launch the installed Android sample app and print a log snapshot
	@./scripts/android-test.sh launch

android-logs: ## Follow Android sample app logs
	@./scripts/android-test.sh logs

android-test: ## Build, deploy, and test Android SDK sample app (use CMD= for subcommands: build|deploy|register|restart|logs|snapshot)
	@./scripts/android-test.sh $(CMD)

# =============================================================================
# USB Android Device Development (physical device via USB)
# =============================================================================

usb-android-setup: ## Set up USB device: port forwarding + assetlinks + config
	@./scripts/setup-android.sh --package $(APP_PACKAGE)
	@./scripts/usb-android-test.sh setup
	@./scripts/usb-android-test.sh config

usb-android-config: ## Generate USB-specific VC config overlay (localhost via adb reverse)
	@./scripts/usb-android-test.sh config

usb-android-up: usb-android-config ## Start USB Android overlay services (SDK_REBUILD=yes to rebuild)
	@docker network inspect e2e-test-network >/dev/null 2>&1 || docker network create e2e-test-network
	$(if $(call _truthy,$(SDK_REBUILD)),@./scripts/usb-android-test.sh rebuild)
	@docker compose -f docker-compose.test.yml \
		-f docker-compose.vc-services.yml \
		-f docker-compose.go-trust.yml \
		-f docker-compose.go-trust-allow.yml \
		$(if $(call _truthy,$(R2PS)),-f docker-compose.r2ps.yml) \
		-f docker-compose.android-usb.yml \
		up -d
	@./scripts/usb-android-test.sh setup

usb-android-down: ## Stop USB Android overlay services
	@docker compose -f docker-compose.test.yml \
		-f docker-compose.vc-services.yml \
		-f docker-compose.go-trust.yml \
		-f docker-compose.go-trust-allow.yml \
		$(if $(call _truthy,$(R2PS)),-f docker-compose.r2ps.yml) \
		-f docker-compose.android-usb.yml \
		down
	@./scripts/usb-android-test.sh teardown

usb-android-full: ## Run full USB Android test flow (setup + build + deploy + register + launch)
	@SDK_REBUILD=$(SDK_REBUILD) ./scripts/usb-android-test.sh full

usb-android-restart: ## Restart USB Android test services and relaunch app
	@./scripts/usb-android-test.sh restart

usb-android-launch: ## Launch the installed Android sample app on USB device
	@./scripts/usb-android-test.sh launch

usb-android-logs: ## Follow Android sample app logs from USB device
	@./scripts/usb-android-test.sh logs

usb-android-status: ## Show USB device info, port forwarding, and app status
	@./scripts/usb-android-test.sh status

usb-android-test: ## Build, deploy, and test on USB device (use CMD= for subcommands)
	@./scripts/usb-android-test.sh $(CMD)

usb-android-test-wsca: ## Run WSCA lifecycle conformance tests on USB device (R2PS_URL / FIDO2_ENABLED)
	@./scripts/usb-android-test.sh test-wsca

# =============================================================================
# Conformance Results
# =============================================================================

CONFORMANCE_RESULTS_DIR ?= ./conformance-results
SIROS_CONFORMANCE_DIR ?= ../siros-conformance

usb-android-conformance: ## Run OID4VCI/VP conformance tests on USB device (PLAN=vci|vp|all)
	node run-android-usb-conformance.mjs --plan $(or $(PLAN),all) --results-dir $(CONFORMANCE_RESULTS_DIR)

publish-conformance-results: ## Publish conformance results to siros-conformance GitHub Pages
	@test -d "$(CONFORMANCE_RESULTS_DIR)" || { echo "ERROR: No results in $(CONFORMANCE_RESULTS_DIR). Run 'make usb-android-conformance' first."; exit 1; }
	@test -d "$(SIROS_CONFORMANCE_DIR)" || { echo "ERROR: siros-conformance repo not found at $(SIROS_CONFORMANCE_DIR)"; exit 1; }
	cd $(SIROS_CONFORMANCE_DIR) && node scripts/publish-pages.mjs \
		$(abspath $(CONFORMANCE_RESULTS_DIR)) \
		--run-id "local-$$(date +%Y%m%d-%H%M%S)" \
		--run-url ""
