.DEFAULT_GOAL := help

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
PAPER_DIR := $(ROOT)/IEEE_conference_template
PAPER_TOOLS := $(ROOT)/scripts/paper
BUILD_DIR := $(PAPER_DIR)/build
PAPER_BUILD := $(BUILD_DIR)/paper_zh
PAPER_EN_BUILD := $(BUILD_DIR)/paper_en
NATIVE_DIR := $(ROOT)/ZAC_zzx/native
PYTHON ?= python3
JOBS ?= 4
export PYTHONDONTWRITEBYTECODE := 1
export TMPDIR := $(BUILD_DIR)/tmp

.PHONY: help build-dirs paper paper-clean paper-check paper-test paper-preview paper-en paper-en-check paper-en-preview native-build native-test native-wheel

help:
	@printf '%s\n' 'make paper          Build and verify the Chinese paper' 'make paper-clean    Build and verify the clean Chinese paper' 'make paper-check    Verify existing paper output without recompiling' 'make paper-test     Run paper build, numeric and figure-data tests' 'make paper-preview  Render pages and contact sheet after paper verification' 'make native-build   Configure/build C++17 backend in IEEE_conference_template/build/native/ctest' 'make native-test    Build and run native CTest checks' 'make native-wheel   Build a wheel in IEEE_conference_template/build/native/dist (does not install it)' 'Override Python with PYTHON=/path/to/environment/bin/python'
	@printf '%s\n' 'make paper-en       Build the full English counterpart after shared Chinese QA' 'make paper-en-check Verify English output and Chinese-English correspondence' 'make paper-en-preview Render English pages and contact sheet'

build-dirs:
	@mkdir -p "$(PAPER_BUILD)" "$(BUILD_DIR)/tmp"

paper: build-dirs
	@$(PYTHON) "$(PAPER_TOOLS)/generate_horizon_extension_values.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/generate_physical_ga_initial_values.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/generate_loss_decomposition.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/generate_horizon_figure.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/generate_argument_values.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/generate_fidelity_distribution.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/generate_initial_lookahead_values.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/generate_initial_lookahead_extension_values.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/verify_paper_zh.py" --compile --json-output "$(PAPER_BUILD)/final_paper_qa.json" > "$(PAPER_BUILD)/verification.stdout.json" || { tail -n 60 "$(PAPER_BUILD)/verification.stdout.json"; exit 1; }
	@printf '%s\n' 'Paper QA: PASS' 'PDF: $(PAPER_BUILD)/paper_zh.pdf' 'Report: $(PAPER_BUILD)/final_paper_qa.json'

paper-clean: build-dirs
	@$(PYTHON) "$(PAPER_TOOLS)/generate_physical_ga_initial_values.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/generate_loss_decomposition.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/generate_horizon_figure.py" --check
	@mkdir -p "$(PAPER_BUILD)/clean"
	@$(PYTHON) "$(PAPER_TOOLS)/verify_paper_zh.py" --compile --revision-mode clean --json-output "$(PAPER_BUILD)/clean/final_paper_qa.json" > "$(PAPER_BUILD)/clean/verification.stdout.json" || { tail -n 60 "$(PAPER_BUILD)/clean/verification.stdout.json"; exit 1; }
	@printf '%s\n' 'Clean paper QA: PASS' 'PDF: $(PAPER_BUILD)/clean/paper_zh.pdf'

paper-check: build-dirs
	@$(PYTHON) "$(PAPER_TOOLS)/generate_horizon_extension_values.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/generate_physical_ga_initial_values.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/generate_loss_decomposition.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/generate_horizon_figure.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/generate_argument_values.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/generate_fidelity_distribution.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/generate_initial_lookahead_values.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/generate_initial_lookahead_extension_values.py" --check
	@$(PYTHON) "$(PAPER_TOOLS)/verify_paper_zh.py" --json-output "$(PAPER_BUILD)/final_paper_qa.json" > "$(PAPER_BUILD)/verification.stdout.json" || { tail -n 60 "$(PAPER_BUILD)/verification.stdout.json"; exit 1; }
	@printf '%s\n' 'Paper QA: PASS (existing output)'

paper-test: build-dirs
	$(PYTHON) -m unittest discover -s "$(PAPER_TOOLS)" -p 'test_*.py'
	$(PYTHON) -m pytest -q -p no:cacheprovider "$(PAPER_TOOLS)/test_prepare_experimental_summary.py"
	$(PYTHON) -m unittest discover -s "$(ROOT)/scripts" -p 'test_prepare_overleaf_sync.py'
	$(PYTHON) -m unittest discover -s "$(ROOT)/scripts" -p 'test_audit_paper_revisions.py'

paper-preview: paper
	$(PYTHON) "$(ROOT)/scripts/render_paper_preview.py"

paper-en: paper
	@mkdir -p "$(PAPER_EN_BUILD)"
	@$(PYTHON) "$(PAPER_TOOLS)/verify_paper_en.py" --compile > "$(PAPER_EN_BUILD)/verification.stdout.json" || { tail -n 70 "$(PAPER_EN_BUILD)/verification.stdout.json"; exit 1; }
	@printf '%s\n' 'English paper QA: PASS' 'PDF: $(PAPER_EN_BUILD)/paper_en.pdf' 'Report: $(PAPER_EN_BUILD)/final_paper_qa.json'

paper-en-check: build-dirs
	@mkdir -p "$(PAPER_EN_BUILD)"
	@$(PYTHON) "$(PAPER_TOOLS)/verify_paper_en.py" > "$(PAPER_EN_BUILD)/verification.stdout.json" || { tail -n 70 "$(PAPER_EN_BUILD)/verification.stdout.json"; exit 1; }
	@printf '%s\n' 'English paper QA: PASS (existing output)'

paper-en-preview: paper-en
	$(PYTHON) "$(ROOT)/scripts/render_paper_preview.py" --language en

native-build: build-dirs
	cmake -S "$(NATIVE_DIR)" -B "$(BUILD_DIR)/native/ctest" -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON -DPython_EXECUTABLE="$$($(PYTHON) -c 'import sys; print(sys.executable)')" -Dpybind11_DIR="$$($(PYTHON) -m pybind11 --cmakedir)"
	cmake --build "$(BUILD_DIR)/native/ctest" --config Release --parallel $(JOBS)

native-test: native-build
	ctest --test-dir "$(BUILD_DIR)/native/ctest" --output-on-failure

native-wheel: build-dirs
	CMAKE_GENERATOR="Unix Makefiles" $(PYTHON) -m build "$(NATIVE_DIR)" --wheel --no-isolation --outdir "$(BUILD_DIR)/native/dist" -Cbuild-dir="$(BUILD_DIR)/native/wheel/{wheel_tag}"
