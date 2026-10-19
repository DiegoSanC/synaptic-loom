# Use the project venv if it exists (.venv); otherwise fall back to the system python3.
PYTHON := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

.PHONY: test contract serve install

test:
	$(PYTHON) -m pytest server/tests client/tests

contract:
	$(PYTHON) contract/verify.py

serve:
	$(PYTHON) -m uvicorn server.app:app --reload

install:
	bash client/install.sh
