# Usa el venv del proyecto si existe (.venv), si no cae a python3 del sistema.
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
