PYTHON ?= python3
CONFIG ?= config/policy.yaml
OPS = $(PYTHON) ops/gateway_ops.py

.PHONY: help validate test ops-test preflight mac-preflight mac-plan status drain backup release rollback restore
help:
	@echo 'validate/preflight/status are read-only; tests use mocks and isolated database fixtures.'
	@echo 'mac-preflight checks local Mac/mock prerequisites; mac-plan only prints reviewed manual startup commands.'
	@echo 'drain/backup/release/rollback/restore print plans only; use ops/gateway_ops.py for explicit execution.'
validate:
	$(PYTHON) -m gateway.config validate --config $(CONFIG)
test:
	$(PYTHON) -m pytest tests ops -q
ops-test:
	$(PYTHON) -m pytest ops -q
preflight:
	$(OPS) preflight $(ARGS)
mac-preflight:
	$(PYTHON) ops/mac_preflight.py check
mac-plan:
	$(PYTHON) ops/mac_preflight.py plan
status:
	$(OPS) status $(ARGS)
drain:
	$(OPS) drain $(ARGS)
backup:
	$(OPS) backup $(ARGS)
release:
	$(OPS) release --candidate $(CONFIG) $(ARGS)
rollback:
	$(OPS) rollback $(ARGS)
restore:
	$(OPS) restore $(ARGS)

.PHONY: sdk-validate export-diagnostics acceptance
sdk-validate:
	$(PYTHON) -m gateway.config validate --config config/policy.sdk.mock.yaml
export-diagnostics:
	$(PYTHON) ops/diagnostic_export.py $(ARGS)
acceptance:
	$(PYTHON) tests/run_postgres_suite.py $(ARGS)
