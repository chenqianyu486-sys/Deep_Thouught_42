# Makefile for FPGA Design Optimization Agent

# Configuration
PYTHON := python3
PIP := $(PYTHON) -m pip

# Vivado executable - can be overridden with: make setup VIVADO_EXEC=/path/to/vivado
VIVADO_EXEC ?= vivado
export VIVADO_EXEC

# Set JAVA_HOME from PATH or Vivado if not already set
# Python RapidWright may need JAVA_HOME to be set, but often users only have `java` on PATH
# See: https://www.rapidwright.io/docs/Install.html#using-java-distributed-with-vivado
ifndef JAVA_HOME
  JAVA_PATH := $(shell command -v java 2>/dev/null)
  ifneq ($(JAVA_PATH),)
    # Found java on PATH - resolve symlinks and derive JAVA_HOME
    # Try readlink -f (Linux), fall back to direct path (macOS/others)
    REAL_JAVA_PATH := $(shell readlink -f "$(JAVA_PATH)" 2>/dev/null || readlink "$(JAVA_PATH)" 2>/dev/null || echo "$(JAVA_PATH)")
    # java is at $JAVA_HOME/bin/java, so go up two directories
    export JAVA_HOME := $(shell dirname $(shell dirname $(REAL_JAVA_PATH)))
  else
    # java not on PATH - try to find Java bundled with Vivado
    # Vivado includes Java at: <VIVADO_ROOT>/tps/lnx64/jre11*/bin/java
    VIVADO_PATH := $(shell command -v $(VIVADO_EXEC) 2>/dev/null)
    ifneq ($(VIVADO_PATH),)
      VIVADO_ROOT := $(shell dirname $(shell dirname $(VIVADO_PATH)))
      VIVADO_JAVA := $(shell ls $(VIVADO_ROOT)/tps/lnx64/jre11*/bin/java 2>/dev/null | head -n 1)
      ifneq ($(VIVADO_JAVA),)
        export JAVA_HOME := $(shell dirname $(shell dirname $(VIVADO_JAVA)))
        export PATH := $(JAVA_HOME)/bin:$(PATH)
      endif
    endif
  endif
endif

# RapidWright submodule path and classpath
# Points the Python rapidwright package to use the local RapidWright source
# See: https://www.rapidwright.io/docs/Install_RapidWright_as_a_Python_PIP_Package.html#java-development-and-python
RAPIDWRIGHT_PATH := $(CURDIR)/RapidWright
export RAPIDWRIGHT_PATH
export CLASSPATH := $(RAPIDWRIGHT_PATH)/bin:$(RAPIDWRIGHT_PATH)/jars/*:$(RAPIDWRIGHT_PATH)/build/libs/rapidwright.jar

# Vivado license file (set to empty to skip)
XILINXD_LICENSE_FILE ?= $(HOME)/.Xilinx/Xilinx.lic
export XILINXD_LICENSE_FILE

# Auto-discover Vivado if not on PATH (AWS AMI: /tools/Xilinx/Vivado/2025.1/)
ifeq ($(shell command -v $(VIVADO_EXEC) 2>/dev/null),)
  VIVADO_CANDIDATE := $(shell ls -d /tools/Xilinx/Vivado/2025.*/bin/vivado 2>/dev/null | head -n 1)
  ifneq ($(VIVADO_CANDIDATE),)
    VIVADO_EXEC := $(VIVADO_CANDIDATE)
    export VIVADO_EXEC
  endif
endif

# Example DCPs to download
EXAMPLE_DCP_1 := demo_corundum_25g_misses_timing.dcp
EXAMPLE_DCP_2 := logicnets_jscl.dcp
DCP_URL_BASE := http://data.rapidwright.io/example-dcps

# tiktoken pre-cache (avoids runtime download failures behind firewalls/proxies)
TIKTOKEN_CACHE_DIR ?= $(HOME)/.cache/tiktoken
TIKTOKEN_BPE_URL := https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken
TIKTOKEN_BPE_CACHE_KEY := 9b5ad71b2ce5302211f9c61530b329a4922fc6a4

# Colors for output
COLOR_GREEN := \033[0;32m
COLOR_YELLOW := \033[0;33m
COLOR_RED := \033[0;31m
COLOR_BLUE := \033[0;34m
COLOR_RESET := \033[0m

.PHONY: setup build-rapidwright run_optimizer run_optimizer_dashboard test test-unit test-skills test-quick validate validate_demo validate-submission run-submission clean veryclean help

# Default target
help:
	@echo "FPGA Design Optimization Agent - Makefile"
	@echo ""
	@echo "Competition workflow (FPL'26):"
	@echo "  make setup SKIP_EXAMPLES=1                        # One-time setup on AWS instance"
	@echo "  make run_optimizer DCP=fpl26_contest_benchmarks/benchmark1.dcp"
	@echo "  make run_optimizer DCP=fpl26_contest_benchmarks/benchmark2.dcp"
	@echo "  make validate-submission DCP=fpl26_contest_benchmarks/benchmark1.dcp"
	@echo ""
	@echo "Available targets:"
	@echo "  setup                - Install dependencies, build RapidWright, download example DCPs"
	@echo "  build-rapidwright    - Build RapidWright from source (git submodule)"
	@echo "  run_optimizer        - Run optimizer on a DCP file (state-machine, default)"
	@echo "  run_optimizer_dashboard - Run optimizer with web dashboard (real-time monitoring)"
	@echo "  run_test_v2          - Run test mode (validate MCP tools/skills, no LLM)"
	@echo "  run_skill_test_v2    - Run skill-only test (quick validation, no place/route)"
	@echo "  run_init_analysis    - Run init analysis only: extract data + verify dashboard completeness (no LLM)"
	@echo "  test                 - Run all unit tests (pytest, fast mode)"
	@echo "  test-unit            - Run optimizer/tests/ unit tests (pytest)"
	@echo "  test-skills          - Run all skill framework & strategy tests"
	@echo "  test-quick           - Run only pure-function tests"
	@echo "  validate             - Validate functional equivalence between two DCPs"
	@echo "  validate_demo        - Run validation demo (self-check)"
	@echo "  validate-submission  - Find and validate optimized DCP against original"
	@echo "  clean                - Remove generated files (run directories, logs, Vivado outputs)"
	@echo "  veryclean            - Remove all generated files including example DCPs"
	@echo ""
	@echo "Usage examples:"
	@echo "  make setup"
	@echo "  make setup SKIP_EXAMPLES=1"
	@echo "  make setup VIVADO_EXEC=/tools/Xilinx/Vivado/2025.1/bin/vivado"
	@echo "  make run_optimizer DCP=logicnets_jscl.dcp"
	@echo "  make run_optimizer_dashboard DCP=logicnets_jscl.dcp  # With web dashboard"
	@echo "  make run_optimizer_dashboard DCP=logicnets_jscl.dcp DASHBOARD_PORT=9090  # Custom port"
	@echo "  make run_test_v2 DCP=demo_corundum_25g_misses_timing.dcp"
	@echo "  make run_skill_test_v2 DCP=demo_corundum_25g_misses_timing.dcp"
	@echo "  make run_init_analysis DCP=demo_corundum_25g_misses_timing.dcp"
	@echo "  make validate GOLDEN=design.dcp REVISED=design_optimized.dcp"
	@echo "  make validate GOLDEN=design.dcp REVISED=design_optimized.dcp VECTORS=50000"
	@echo "  make validate_demo"
	@echo "  make validate-submission DCP=logicnets_jscl.dcp"
	@echo "  make clean"
	@echo ""
	@echo "Environment variables:"
	@echo "  VIVADO_EXEC          - Path to Vivado executable (default: vivado, auto-discovered)"
	@echo "  JAVA_HOME            - Java installation directory (auto-detected from PATH if not set)"
	@echo "  OPENROUTER_API_KEY   - OpenRouter API key (required for run_optimizer, set by organizers)"
	@echo "  DCP                  - Input DCP file for run_optimizer / run_test targets"
	@echo "  DASHBOARD_PORT       - HTTP port for web dashboard (default: 8080)"
	@echo "  MAX_NETS             - Max high fanout nets to optimize in test mode (default: 5)"
	@echo "  SKIP_SKILLS          - Set to 1 to skip skill invocation tests in test mode"
	@echo "  SKIP_EXAMPLES        - Set to 1 to skip example DCP downloads in setup"
	@echo "  GOLDEN               - Golden (reference) DCP for validation"
	@echo "  REVISED              - Revised (optimized) DCP for validation"
	@echo "  VECTORS              - Number of test vectors for validation (default: 200)"
	@echo ""
	@echo "Output structure:"
	@echo "  - Optimized DCP: <input_name>_optimized-<timestamp>.dcp (next to input)"
	@echo "  - Run directory: dcp_optimizer_run-<timestamp>/ (contains all logs)"
	@echo "  - Validation:    /tmp/dcp_validation_*/ (contains simulation logs)"

# Setup target: Install dependencies, check Vivado, set up Java, build RapidWright, download DCPs
setup:
	@printf "$(COLOR_GREEN)===== FPGA Design Optimization Setup =====$(COLOR_RESET)\n"
	@echo ""

	@printf "$(COLOR_YELLOW)[1/9] Checking for wget/curl (required for downloads)...$(COLOR_RESET)\n"
	@if ! command -v wget >/dev/null 2>&1 && ! command -v curl >/dev/null 2>&1; then \
		printf "$(COLOR_RED)✗ Neither wget nor curl found$(COLOR_RESET)\n"; \
		echo "Please install wget or curl:"; \
		echo "  sudo apt-get install -y wget curl"; \
		exit 1; \
	fi
	@printf "$(COLOR_GREEN)✓ wget/curl available$(COLOR_RESET)\n"
	@echo ""

	@printf "$(COLOR_YELLOW)[2/9] Installing Python dependencies...$(COLOR_RESET)\n"
	@EXTERNALLY_MANAGED=$$($(PYTHON) -c "import sysconfig; print(sysconfig.get_path('stdlib') + '/EXTERNALLY-MANAGED')") && \
	if [ -f "$$EXTERNALLY_MANAGED" ]; then \
		printf "$(COLOR_YELLOW)PEP 668 detected (Ubuntu 22.04+), using --break-system-packages$(COLOR_RESET)\n"; \
		$(PYTHON) -m pip install --break-system-packages -r requirements.txt; \
	else \
		$(PIP) install -r requirements.txt; \
	fi
	@printf "$(COLOR_GREEN)✓ Python dependencies installed$(COLOR_RESET)\n"
	@echo ""

	@printf "$(COLOR_YELLOW)[3/9] Pre-caching tiktoken encoding...$(COLOR_RESET)\n"
	@mkdir -p $(TIKTOKEN_CACHE_DIR)
	@if [ -f "$(TIKTOKEN_CACHE_DIR)/$(TIKTOKEN_BPE_CACHE_KEY)" ]; then \
		printf "$(COLOR_GREEN)✓ tiktoken cl100k_base already cached$(COLOR_RESET)\n"; \
	else \
		echo "Downloading cl100k_base.tiktoken..."; \
		if wget -q --timeout=30 -O "$(TIKTOKEN_CACHE_DIR)/$(TIKTOKEN_BPE_CACHE_KEY).tmp" "$(TIKTOKEN_BPE_URL)" 2>/dev/null || \
		   curl -sS --connect-timeout 30 -o "$(TIKTOKEN_CACHE_DIR)/$(TIKTOKEN_BPE_CACHE_KEY).tmp" "$(TIKTOKEN_BPE_URL)" 2>/dev/null; then \
			mv "$(TIKTOKEN_CACHE_DIR)/$(TIKTOKEN_BPE_CACHE_KEY).tmp" "$(TIKTOKEN_CACHE_DIR)/$(TIKTOKEN_BPE_CACHE_KEY)"; \
			printf "$(COLOR_GREEN)✓ tiktoken cl100k_base cached$(COLOR_RESET)\n"; \
		else \
			printf "$(COLOR_YELLOW)⚠ tiktoken download failed (non-fatal, will retry at runtime)$(COLOR_RESET)\n"; \
			rm -f "$(TIKTOKEN_CACHE_DIR)/$(TIKTOKEN_BPE_CACHE_KEY).tmp"; \
		fi; \
	fi
	@echo ""

	@printf "$(COLOR_YELLOW)[4/9] Checking Vivado...$(COLOR_RESET)\n"
	@if command -v $(VIVADO_EXEC) >/dev/null 2>&1; then \
		printf "$(COLOR_GREEN)✓ Vivado found: %s$(COLOR_RESET)\n" "$$(command -v $(VIVADO_EXEC))"; \
		$(VIVADO_EXEC) -version | head -n 1; \
	else \
		printf "$(COLOR_RED)✗ Vivado not found on PATH$(COLOR_RESET)\n"; \
		echo ""; \
		echo "Please either:"; \
		echo "  1. Source Vivado settings: source /path/to/Vivado/*/settings64.sh"; \
		echo "  2. Specify Vivado path: make setup VIVADO_EXEC=/path/to/vivado"; \
		exit 1; \
	fi
	@echo ""
	
	@printf "$(COLOR_YELLOW)[5/9] Checking Java...$(COLOR_RESET)\n"
	@if command -v java >/dev/null 2>&1; then \
		printf "$(COLOR_GREEN)✓ Java found: %s$(COLOR_RESET)\n" "$$(command -v java)"; \
		java -version 2>&1 | head -n 1; \
	else \
		printf "$(COLOR_YELLOW)⚠ Java not found on PATH$(COLOR_RESET)\n"; \
		echo "Attempting to locate Java from Vivado installation..."; \
		VIVADO_PATH=$$(command -v $(VIVADO_EXEC)); \
		if [ -n "$$VIVADO_PATH" ]; then \
			VIVADO_BIN_DIR=$$(dirname $$VIVADO_PATH); \
			VIVADO_ROOT=$$(dirname $$VIVADO_BIN_DIR); \
			VIVADO_JAVA="$$VIVADO_ROOT/tps/lnx64/jre11*/bin/java"; \
			if ls $$VIVADO_JAVA >/dev/null 2>&1; then \
				JAVA_FOUND=$$(ls $$VIVADO_JAVA | head -n 1); \
				printf "$(COLOR_GREEN)✓ Found Java in Vivado: %s$(COLOR_RESET)\n" "$$JAVA_FOUND"; \
				echo ""; \
				printf "$(COLOR_YELLOW)NOTE: Set JAVA_HOME before running optimizer:$(COLOR_RESET)\n"; \
				JAVA_HOME_DIR=$$(dirname $$(dirname $$JAVA_FOUND)); \
				echo "  export JAVA_HOME=$$JAVA_HOME_DIR"; \
				echo "  export PATH=\$$JAVA_HOME/bin:\$$PATH"; \
			else \
				printf "$(COLOR_RED)✗ Could not find Java in Vivado installation$(COLOR_RESET)\n"; \
				echo "Please install Java 11 or later"; \
				exit 1; \
			fi; \
		else \
			printf "$(COLOR_RED)✗ Cannot locate Java$(COLOR_RESET)\n"; \
			echo "Please install Java 11 or later"; \
			exit 1; \
		fi; \
	fi
	@echo ""
	
	@printf "$(COLOR_YELLOW)[6/9] Building RapidWright from source...$(COLOR_RESET)\n"
	@$(MAKE) build-rapidwright
	@echo ""

	@printf "$(COLOR_YELLOW)[7/9] Patching rapidwright and skill imports...$(COLOR_RESET)\n"
	@$(PYTHON) scripts/patch_rapidwright.py 2>/dev/null; \
		if [ $$? -eq 0 ]; then \
			printf "$(COLOR_GREEN)  ✓ rapidwright JPype classpath patched$(COLOR_RESET)\n"; \
		else \
			printf "$(COLOR_YELLOW)  ⚠ rapidwright patch skipped (non-critical)$(COLOR_RESET)\n"; \
		fi
	@$(PYTHON) scripts/patch_skill_imports.py 2>/dev/null; \
		if [ $$? -eq 0 ]; then \
			printf "$(COLOR_GREEN)  ✓ skill DesignTools imports patched$(COLOR_RESET)\n"; \
		else \
			printf "$(COLOR_YELLOW)  ⚠ skill import patch skipped (non-critical)$(COLOR_RESET)\n"; \
		fi
	@echo ""

	@printf "$(COLOR_YELLOW)[8/9] Downloading example DCP: $(EXAMPLE_DCP_1)...$(COLOR_RESET)\n"
	@if [ "$(SKIP_EXAMPLES)" = "1" ]; then \
		printf "$(COLOR_YELLOW)Skipping example DCP downloads (SKIP_EXAMPLES=1)$(COLOR_RESET)\n"; \
	elif [ -f "$(EXAMPLE_DCP_1)" ]; then \
		printf "$(COLOR_GREEN)✓ $(EXAMPLE_DCP_1) already exists$(COLOR_RESET)\n"; \
	else \
		if command -v wget >/dev/null 2>&1; then \
			wget -q --show-progress $(DCP_URL_BASE)/$(EXAMPLE_DCP_1); \
			printf "$(COLOR_GREEN)✓ Downloaded $(EXAMPLE_DCP_1)$(COLOR_RESET)\n"; \
		else \
			curl -# -O $(DCP_URL_BASE)/$(EXAMPLE_DCP_1); \
			printf "$(COLOR_GREEN)✓ Downloaded $(EXAMPLE_DCP_1)$(COLOR_RESET)\n"; \
		fi; \
	fi
	@echo ""

	@printf "$(COLOR_YELLOW)[9/9] Downloading example DCP: $(EXAMPLE_DCP_2)...$(COLOR_RESET)\n"
	@if [ "$(SKIP_EXAMPLES)" = "1" ]; then \
		printf "$(COLOR_YELLOW)Skipping example DCP downloads (SKIP_EXAMPLES=1)$(COLOR_RESET)\n"; \
	elif [ -f "$(EXAMPLE_DCP_2)" ]; then \
		printf "$(COLOR_GREEN)✓ $(EXAMPLE_DCP_2) already exists$(COLOR_RESET)\n"; \
	else \
		if command -v wget >/dev/null 2>&1; then \
			wget -q --show-progress $(DCP_URL_BASE)/$(EXAMPLE_DCP_2); \
			printf "$(COLOR_GREEN)✓ Downloaded $(EXAMPLE_DCP_2)$(COLOR_RESET)\n"; \
		else \
			curl -# -O $(DCP_URL_BASE)/$(EXAMPLE_DCP_2); \
			printf "$(COLOR_GREEN)✓ Downloaded $(EXAMPLE_DCP_2)$(COLOR_RESET)\n"; \
		fi; \
	fi
	@echo ""
	
	@printf "$(COLOR_GREEN)===== Setup Complete! =====$(COLOR_RESET)\n"
	@echo ""
	@echo "Next steps - run the optimizer:"
	@echo ""
	@echo "  Test mode (no API key required):"
	@echo "    make run_test DCP=$(EXAMPLE_DCP_1)"
	@echo ""
	@echo "  Full LLM-guided optimizer (requires OPENROUTER_API_KEY):"
	@echo "    make run_optimizer DCP=$(EXAMPLE_DCP_1)"
	@echo ""
	@echo "Output will be in:"
	@echo "  - Optimized DCP: <input_name>_optimized-<timestamp>.dcp"
	@echo "  - Run logs: dcp_optimizer_run-<timestamp>/"
	@echo ""

# Build RapidWright from source (git submodule)
build-rapidwright:
	@printf "$(COLOR_YELLOW)Building RapidWright from source...$(COLOR_RESET)\n"
	@if [ ! -f "$(RAPIDWRIGHT_PATH)/gradlew" ]; then \
		printf "$(COLOR_YELLOW)Initializing RapidWright git submodule...$(COLOR_RESET)\n"; \
		git submodule update --init RapidWright; \
	fi
	@cd "$(RAPIDWRIGHT_PATH)" && chmod +x gradlew && JAVA_HOME=$$JAVA_HOME ./gradlew jar -p "$(RAPIDWRIGHT_PATH)"
	@printf "$(COLOR_GREEN)✓ RapidWright JAR built$(COLOR_RESET)\n"
	@printf "$(COLOR_YELLOW)  Installing RapidWright Python package from local source...$(COLOR_RESET)\n"
	@EXTERNALLY_MANAGED=$$($(PYTHON) -c "import sysconfig; print(sysconfig.get_path('stdlib') + '/EXTERNALLY-MANAGED')") && \
	if [ -f "$$EXTERNALLY_MANAGED" ]; then \
		$(PYTHON) -m pip install --break-system-packages -e "$(RAPIDWRIGHT_PATH)/python/"; \
	else \
		$(PIP) install -e "$(RAPIDWRIGHT_PATH)/python/"; \
	fi
	@printf "$(COLOR_GREEN)✓ RapidWright Python package installed$(COLOR_RESET)\n"
	@printf "$(COLOR_GREEN)  RAPIDWRIGHT_PATH=$(RAPIDWRIGHT_PATH)$(COLOR_RESET)\n"
	@printf "$(COLOR_GREEN)  CLASSPATH=$(CLASSPATH)$(COLOR_RESET)\n"

# Run optimizer target: Run dcp_optimizer.py (output DCP name generated automatically)
run_optimizer:
	@if [ -z "$(DCP)" ]; then \
		printf "$(COLOR_RED)Error: DCP variable not set$(COLOR_RESET)\n"; \
		echo "Usage: make run_optimizer DCP=input.dcp"; \
		exit 1; \
	fi
	@if [ ! -f "$(DCP)" ]; then \
		printf "$(COLOR_RED)Error: DCP file not found: $(DCP)$(COLOR_RESET)\n"; \
		exit 1; \
	fi
	@if [ -z "$(OPENROUTER_API_KEY)" ]; then \
		printf "$(COLOR_RED)Error: OPENROUTER_API_KEY environment variable not set$(COLOR_RESET)\n"; \
		echo "The contest organizers set this automatically."; \
		echo "For local testing: export OPENROUTER_API_KEY=your_key"; \
		exit 1; \
	fi
	@printf "$(COLOR_GREEN)Running optimizer on $(DCP)...$(COLOR_RESET)\n"
	@# Set up Java from Vivado if Java is not available
	@if ! command -v java >/dev/null 2>&1; then \
		printf "$(COLOR_YELLOW)Java not found on PATH, attempting to use Java from Vivado...$(COLOR_RESET)\n"; \
		VIVADO_PATH=$$(command -v $(VIVADO_EXEC) 2>/dev/null); \
		if [ -n "$$VIVADO_PATH" ]; then \
			VIVADO_BIN_DIR=$$(dirname $$VIVADO_PATH); \
			VIVADO_ROOT=$$(dirname $$VIVADO_BIN_DIR); \
			VIVADO_JAVA="$$VIVADO_ROOT/tps/lnx64/jre11*/bin/java"; \
			if ls $$VIVADO_JAVA >/dev/null 2>&1; then \
				JAVA_FOUND=$$(ls $$VIVADO_JAVA | head -n 1); \
				export JAVA_HOME=$$(dirname $$(dirname $$JAVA_FOUND)); \
				export PATH="$$JAVA_HOME/bin:$$PATH"; \
				printf "$(COLOR_GREEN)Using Java from Vivado: %s$(COLOR_RESET)\n" "$$JAVA_HOME"; \
			fi; \
		fi; \
	fi; \
	echo ""; \
	TIKTOKEN_CACHE_DIR=$(TIKTOKEN_CACHE_DIR) $(PYTHON) dcp_optimizer.py "$(DCP)"

# Run optimizer with web dashboard (real-time monitoring)
DASHBOARD_PORT ?= 8080
run_optimizer_dashboard:
	@if [ -z "$(DCP)" ]; then \
		printf "$(COLOR_RED)Error: DCP variable not set$(COLOR_RESET)\n"; \
		echo "Usage: make run_optimizer_dashboard DCP=input.dcp [DASHBOARD_PORT=8080]"; \
		exit 1; \
	fi
	@if [ ! -f "$(DCP)" ]; then \
		printf "$(COLOR_RED)Error: DCP file not found: $(DCP)$(COLOR_RESET)\n"; \
		exit 1; \
	fi
	@if [ -z "$(OPENROUTER_API_KEY)" ]; then \
		printf "$(COLOR_RED)Error: OPENROUTER_API_KEY environment variable not set$(COLOR_RESET)\n"; \
		echo "The contest organizers set this automatically."; \
		echo "For local testing: export OPENROUTER_API_KEY=your_key"; \
		exit 1; \
	fi
	@printf "$(COLOR_GREEN)Running optimizer with dashboard on $(DCP)...$(COLOR_RESET)\n"
	@printf "$(COLOR_GREEN)Dashboard: http://localhost:$(DASHBOARD_PORT)$(COLOR_RESET)\n"
	@# Set up Java from Vivado if Java is not available
	@if ! command -v java >/dev/null 2>&1; then \
		printf "$(COLOR_YELLOW)Java not found on PATH, attempting to use Java from Vivado...$(COLOR_RESET)\n"; \
		VIVADO_PATH=$$(command -v $(VIVADO_EXEC) 2>/dev/null); \
		if [ -n "$$VIVADO_PATH" ]; then \
			VIVADO_BIN_DIR=$$(dirname $$VIVADO_PATH); \
			VIVADO_ROOT=$$(dirname $$VIVADO_BIN_DIR); \
			VIVADO_JAVA="$$VIVADO_ROOT/tps/lnx64/jre11*/bin/java"; \
			if ls $$VIVADO_JAVA >/dev/null 2>&1; then \
				JAVA_FOUND=$$(ls $$VIVADO_JAVA | head -n 1); \
				export JAVA_HOME=$$(dirname $$(dirname $$JAVA_FOUND)); \
				export PATH="$$JAVA_HOME/bin:$$PATH"; \
				printf "$(COLOR_GREEN)Using Java from Vivado: %s$(COLOR_RESET)\n" "$$JAVA_HOME"; \
			fi; \
		fi; \
	fi; \
	echo ""; \
	TIKTOKEN_CACHE_DIR=$(TIKTOKEN_CACHE_DIR) $(PYTHON) dcp_optimizer.py "$(DCP)" --dashboard --dashboard-port $(DASHBOARD_PORT)

# Run v2 test mode: validate MCP tools and skills via state-machine infrastructure (no LLM)
run_test_v2:
	@if [ -z "$(DCP)" ]; then \
		printf "$(COLOR_RED)Error: DCP variable not set$(COLOR_RESET)\n"; \
		echo "Usage: make run_test_v2 DCP=input.dcp [MAX_NETS=5]"; \
		echo ""; \
		echo "Supported example DCPs:"; \
		echo "  make run_test_v2 DCP=demo_corundum_25g_misses_timing.dcp   # High fanout optimization"; \
		echo "  make run_test_v2 DCP=logicnets_jscl.dcp                    # Pblock optimization"; \
		exit 1; \
	fi
	@if [ ! -f "$(DCP)" ]; then \
		printf "$(COLOR_RED)Error: DCP file not found: $(DCP)$(COLOR_RESET)\n"; \
		exit 1; \
	fi
	@printf "$(COLOR_GREEN)Running v2 TEST MODE on $(DCP)...$(COLOR_RESET)\n"
	@# Set up Java from Vivado if Java is not available
	@if ! command -v java >/dev/null 2>&1; then \
		printf "$(COLOR_YELLOW)Java not found on PATH, attempting to use Java from Vivado...$(COLOR_RESET)\n"; \
		VIVADO_PATH=$$(command -v $(VIVADO_EXEC) 2>/dev/null); \
		if [ -n "$$VIVADO_PATH" ]; then \
			VIVADO_BIN_DIR=$$(dirname $$VIVADO_PATH); \
			VIVADO_ROOT=$$(dirname $$VIVADO_BIN_DIR); \
			VIVADO_JAVA="$$VIVADO_ROOT/tps/lnx64/jre11*/bin/java"; \
			if ls $$VIVADO_JAVA >/dev/null 2>&1; then \
				JAVA_FOUND=$$(ls $$VIVADO_JAVA | head -n 1); \
				export JAVA_HOME=$$(dirname $$(dirname $$JAVA_FOUND)); \
				export PATH="$$JAVA_HOME/bin:$$PATH"; \
				printf "$(COLOR_GREEN)Using Java from Vivado: %s$(COLOR_RESET)\n" "$$JAVA_HOME"; \
			fi; \
		fi; \
	fi; \
	echo ""; \
	$(PYTHON) dcp_optimizer.py "$(DCP)" --test-v2 $(if $(MAX_NETS),--max-nets $(MAX_NETS)) $(if $(SKIP_SKILLS),--skip-skills)

# Run v2 skill-only test: quick validation of skill invocations without place/route
run_skill_test_v2:
	@if [ -z "$(DCP)" ]; then \
		printf "$(COLOR_RED)Error: DCP variable not set$(COLOR_RESET)\n"; \
		echo "Usage: make run_skill_test_v2 DCP=input.dcp"; \
		echo ""; \
		echo "Examples:"; \
		echo "  make run_skill_test_v2 DCP=demo_corundum_25g_misses_timing.dcp"; \
		echo "  make run_skill_test_v2 DCP=logicnets_jscl.dcp"; \
		exit 1; \
	fi
	@if [ ! -f "$(DCP)" ]; then \
		printf "$(COLOR_RED)Error: DCP file not found: $(DCP)$(COLOR_RESET)\n"; \
		exit 1; \
	fi
	@printf "$(COLOR_GREEN)Running v2 skill-only test on $(DCP)...$(COLOR_RESET)\n"
	@# Set up Java from Vivado if Java is not available
	@if ! command -v java >/dev/null 2>&1; then \
		printf "$(COLOR_YELLOW)Java not found on PATH, attempting to use Java from Vivado...$(COLOR_RESET)\n"; \
		VIVADO_PATH=$$(command -v $(VIVADO_EXEC) 2>/dev/null); \
		if [ -n "$$VIVADO_PATH" ]; then \
			VIVADO_BIN_DIR=$$(dirname $$VIVADO_PATH); \
			VIVADO_ROOT=$$(dirname $$VIVADO_BIN_DIR); \
			VIVADO_JAVA="$$VIVADO_ROOT/tps/lnx64/jre11*/bin/java"; \
			if ls $$VIVADO_JAVA >/dev/null 2>&1; then \
				JAVA_FOUND=$$(ls $$VIVADO_JAVA | head -n 1); \
				export JAVA_HOME=$$(dirname $$(dirname $$JAVA_FOUND)); \
				export PATH="$$JAVA_HOME/bin:$$PATH"; \
				printf "$(COLOR_GREEN)Using Java from Vivado: %s$(COLOR_RESET)\n" "$$JAVA_HOME"; \
			fi; \
		fi; \
	fi; \
	echo ""; \
	$(PYTHON) dcp_optimizer.py "$(DCP)" --test-v2-only-skills

# Run init_analysis only: extract all design data, build StateSpace dashboard, and verify field completeness (no LLM)
run_init_analysis:
	@if [ -z "$(DCP)" ]; then \
		printf "$(COLOR_RED)Error: DCP variable not set$(COLOR_RESET)\n"; \
		echo "Usage: make run_init_analysis DCP=input.dcp"; \
		echo ""; \
		echo "Runs init analysis (MCP tools only, no LLM) and verifies"; \
		echo "that all 6 StateSpace dashboard modules are populated."; \
		echo ""; \
		echo "Examples:"; \
		echo "  make run_init_analysis DCP=demo_corundum_25g_misses_timing.dcp"; \
		echo "  make run_init_analysis DCP=logicnets_jscl.dcp"; \
		exit 1; \
	fi
	@if [ ! -f "$(DCP)" ]; then \
		printf "$(COLOR_RED)Error: DCP file not found: $(DCP)$(COLOR_RESET)\n"; \
		exit 1; \
	fi
	@printf "$(COLOR_GREEN)Running init analysis + dashboard verification on $(DCP)...$(COLOR_RESET)\n"
	@# Set up Java from Vivado if Java is not available
	@if ! command -v java >/dev/null 2>&1; then \
		printf "$(COLOR_YELLOW)Java not found on PATH, attempting to use Java from Vivado...$(COLOR_RESET)\n"; \
		VIVADO_PATH=$$(command -v $(VIVADO_EXEC) 2>/dev/null); \
		if [ -n "$$VIVADO_PATH" ]; then \
			VIVADO_BIN_DIR=$$(dirname $$VIVADO_PATH); \
			VIVADO_ROOT=$$(dirname $$VIVADO_BIN_DIR); \
			VIVADO_JAVA="$$VIVADO_ROOT/tps/lnx64/jre11*/bin/java"; \
			if ls $$VIVADO_JAVA >/dev/null 2>&1; then \
				JAVA_FOUND=$$(ls $$VIVADO_JAVA | head -n 1); \
				export JAVA_HOME=$$(dirname $$(dirname $$JAVA_FOUND)); \
				export PATH="$$JAVA_HOME/bin:$$PATH"; \
				printf "$(COLOR_GREEN)Using Java from Vivado: %s$(COLOR_RESET)\n" "$$JAVA_HOME"; \
			fi; \
		fi; \
	fi; \
	echo ""; \
	$(PYTHON) dcp_optimizer.py "$(DCP)" --test-init-analysis

# Validation target: Validate functional equivalence between two DCPs
validate:
	@printf "$(COLOR_BLUE)╔══════════════════════════════════════════════════════════════════╗$(COLOR_RESET)\n"
	@printf "$(COLOR_BLUE)║         DCP Equivalence Validation (2-Phase Approach)            ║$(COLOR_RESET)\n"
	@printf "$(COLOR_BLUE)╚══════════════════════════════════════════════════════════════════╝$(COLOR_RESET)\n"
	@echo ""
	@# Check if GOLDEN and REVISED are provided
	@if [ -z "$(GOLDEN)" ]; then \
		printf "$(COLOR_RED)✗ Error: GOLDEN DCP not specified$(COLOR_RESET)\n"; \
		echo "Usage: make validate GOLDEN=<golden.dcp> REVISED=<revised.dcp> [VECTORS=200]"; \
		echo ""; \
		echo "Example:"; \
		echo "  make validate GOLDEN=logicnets_jscl.dcp REVISED=logicnets_jscl_optimized.dcp"; \
		exit 1; \
	fi
	@if [ -z "$(REVISED)" ]; then \
		printf "$(COLOR_RED)✗ Error: REVISED DCP not specified$(COLOR_RESET)\n"; \
		echo "Usage: make validate GOLDEN=<golden.dcp> REVISED=<revised.dcp> [VECTORS=200]"; \
		echo ""; \
		echo "Example:"; \
		echo "  make validate GOLDEN=logicnets_jscl.dcp REVISED=logicnets_jscl_optimized.dcp"; \
		exit 1; \
	fi
	@# Check if files exist
	@if [ ! -f "$(GOLDEN)" ]; then \
		printf "$(COLOR_RED)✗ Error: Golden DCP not found: $(GOLDEN)$(COLOR_RESET)\n"; \
		exit 1; \
	fi
	@if [ ! -f "$(REVISED)" ]; then \
		printf "$(COLOR_RED)✗ Error: Revised DCP not found: $(REVISED)$(COLOR_RESET)\n"; \
		exit 1; \
	fi
	@# Run validation
	@printf "$(COLOR_GREEN)Golden DCP:$(COLOR_RESET)  $(GOLDEN)\n"
	@printf "$(COLOR_GREEN)Revised DCP:$(COLOR_RESET) $(REVISED)\n"
	@printf "$(COLOR_GREEN)Test Vectors:$(COLOR_RESET) $(or $(VECTORS),200)\n"
	@echo ""
	@if [ -n "$(VECTORS)" ]; then \
		$(PYTHON) validate_dcps.py "$(GOLDEN)" "$(REVISED)" --vectors $(VECTORS); \
	else \
		$(PYTHON) validate_dcps.py "$(GOLDEN)" "$(REVISED)"; \
	fi

# Quick validation example using demo DCPs
validate_demo:

# ── Unified test targets ────────────────────────────────────────────────

# Run all unit tests (pytest + skill framework + descriptor validation)
test:
	@printf "\033[0;34m===== Running All Unit Tests =====\033[0m\n"
	@cd "$(CURDIR)" && "$(PYTHON)" -m pytest tests/ optimizer/ -v --ignore=optimizer/test_mode.py --tb=short 2>&1 | tail -10
	@printf "\033[0;34m===== Descriptor Validation =====\033[0m\n"
	@cd "$(CURDIR)" && "$(PYTHON)" -m skills.validate_descriptors 2>&1 | tail -10
	@printf "\033[0;34m===== Skill Framework Tests =====\033[0m\n"
	@cd "$(CURDIR)" && "$(PYTHON)" skills/test_skill_framework.py 2>&1 | tail -5
	@printf "\033[0;34m===== Strategy Tests =====\033[0m\n"
	@cd "$(CURDIR)" && "$(PYTHON)" skills/test_pblock_strategy.py 2>&1 | tail -3
	@cd "$(CURDIR)" && "$(PYTHON)" skills/test_smart_retiming.py 2>&1 | tail -3

# Run optimizer and tests/ unit tests (pytest only, fast ~3s)
test-unit:
	@printf "\033[0;34m===== Running Unit Tests (pytest) =====\033[0m\n"
	@cd "$(CURDIR)" && "$(PYTHON)" -m pytest tests/ optimizer/ -v --ignore=optimizer/test_mode.py --tb=short 2>&1 | tail -10

# Run all skill framework & strategy tests
test-skills:
	@printf "\033[0;34m===== Descriptor Validation =====\033[0m\n"
	@cd "$(CURDIR)" && "$(PYTHON)" -m skills.validate_descriptors 2>&1 | tail -10
	@printf "\033[0;34m===== Skill Framework Tests =====\033[0m\n"
	@cd "$(CURDIR)" && "$(PYTHON)" skills/test_skill_framework.py 2>&1 | tail -10
	@printf "\033[0;34m===== Pblock Strategy =====\033[0m\n"
	@cd "$(CURDIR)" && "$(PYTHON)" skills/test_pblock_strategy.py 2>&1 | tail -5
	@printf "\033[0;34m===== Smart Retiming =====\033[0m\n"
	@cd "$(CURDIR)" && "$(PYTHON)" skills/test_smart_retiming.py 2>&1 | tail -5
	@printf "\033[0;34m===== Net Detour =====\033[0m\n"
	@cd "$(CURDIR)" && "$(PYTHON)" skills/test_net_detour_optimization.py 2>&1 | tail -5

# Run only quick pure-function tests
test-quick:
	@printf "\033[0;34m===== Running Quick Tests (pytest) =====\033[0m\n"
	@cd "$(CURDIR)" && "$(PYTHON)" -m pytest tests/ optimizer/test_pure.py -v --tb=short 2>&1 | tail -10
	@printf "$(COLOR_BLUE)╔══════════════════════════════════════════════════════════════════╗$(COLOR_RESET)\n"
	@printf "$(COLOR_BLUE)║                  Validation Demo (Simulated)                     ║$(COLOR_RESET)\n"
	@printf "$(COLOR_BLUE)╚══════════════════════════════════════════════════════════════════╝$(COLOR_RESET)\n"
	@echo ""
	@echo "This demo validates a DCP against itself (should always PASS)."
	@echo "For real validation, first optimize a design, then validate:"
	@echo ""
	@echo "  1. python dcp_optimizer.py design.dcp --output design_optimized.dcp"
	@echo "  2. make validate GOLDEN=design.dcp REVISED=design_optimized.dcp"
	@echo ""
	@# Check if example DCP exists
	@if [ ! -f "$(EXAMPLE_DCP_2)" ]; then \
		printf "$(COLOR_YELLOW)Example DCP not found, downloading...$(COLOR_RESET)\n"; \
		$(MAKE) download_dcps; \
	fi
	@# For demo, validate DCP against itself (should always pass)
	@printf "$(COLOR_GREEN)Running demo validation (self-check)...$(COLOR_RESET)\n"
	@echo ""
	$(PYTHON) validate_dcps.py "$(EXAMPLE_DCP_2)" "$(EXAMPLE_DCP_2)" --vectors 1000

# Validate optimized DCP against original (competition helper)
validate-submission:
	@if [ -z "$(DCP)" ]; then \
		printf "$(COLOR_RED)Error: DCP variable not set$(COLOR_RESET)\n"; \
		echo "Usage: make validate-submission DCP=input.dcp"; \
		exit 1; \
	fi
	@if [ ! -f "$(DCP)" ]; then \
		printf "$(COLOR_RED)Error: DCP file not found: $(DCP)$(COLOR_RESET)\n"; \
		exit 1; \
	fi
	@INPUT_DIR=$$(dirname "$(DCP)"); \
	INPUT_STEM=$$(basename "$(DCP)" .dcp); \
	OPTIMIZED=$$(ls -t "$$INPUT_DIR"/$$INPUT_STEM"_optimized"*.dcp 2>/dev/null | head -n 1); \
	if [ -z "$$OPTIMIZED" ]; then \
		printf "$(COLOR_RED)Error: No optimized DCP found matching $$INPUT_STEM*_optimized*.dcp$(COLOR_RESET)\n"; \
		exit 1; \
	fi; \
	printf "$(COLOR_GREEN)Validating: $(DCP) vs $$OPTIMIZED$(COLOR_RESET)\n"; \
	$(PYTHON) validate_dcps.py "$(DCP)" "$$OPTIMIZED"

run-submission:
	@echo "Running submission...[Will be implemented later]"
	
# Clean target: Remove run directories and Vivado-generated .Xil directories
clean:
	@printf "$(COLOR_YELLOW)Cleaning generated files...$(COLOR_RESET)\n"
	@# Remove run directories (contain all logs, journals, intermediate files)
	@if ls dcp_optimizer_run-* >/dev/null 2>&1; then \
		rm -rf dcp_optimizer_run-*; \
		echo "Removed dcp_optimizer_run-* directories"; \
	fi
	@# Remove .Xil directories (Vivado generates these outside run directories)
	@if [ -d ".Xil" ]; then \
		rm -rf .Xil; \
		echo "Removed .Xil/"; \
	fi
	@if [ -d "VivadoMCP/.Xil" ]; then \
		rm -rf VivadoMCP/.Xil; \
		echo "Removed VivadoMCP/.Xil/"; \
	fi
	@printf "$(COLOR_GREEN)✓ Clean complete$(COLOR_RESET)\n"
	@echo "Note: Optimized DCP files were preserved"

# Very clean target: Clean + remove __pycache__ and example DCPs
veryclean: clean
	@printf "$(COLOR_YELLOW)Performing deep clean...$(COLOR_RESET)\n"
	@# Remove Python cache
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "Removed __pycache__ directories"
	@# Remove example DCPs
	@rm -f $(EXAMPLE_DCP_1) $(EXAMPLE_DCP_2)
	@echo "Removed example DCPs"
	@printf "$(COLOR_GREEN)✓ Deep clean complete$(COLOR_RESET)\n"
