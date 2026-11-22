.PHONY: install test run all clean

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"

test:
	.venv/bin/pytest -q

run:
	.venv/bin/company-dataset-legacy all

all: install test run

# Removes generated working files only; keeps downloads (data/raw) and the deliverable (data/output).
clean:
	rm -rf .venv .pytest_cache data/cache data/intermediate data/review
	find . -name __pycache__ -prune -exec rm -rf {} +
