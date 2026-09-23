.PHONY: check lint test setup

check: lint test

lint:
	uv run ruff check .
	uv run ruff format --check .

test:
	uv run pytest

# One idempotent entry point for a fresh checkout: uv creates .venv on the
# interpreter in .python-version if it is absent, and a second run is a no-op.
setup:
	uv sync --locked --dev
