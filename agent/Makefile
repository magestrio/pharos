run:
	uv run python -m agent.main

api:
	uv run uvicorn agent.api.server:app --reload

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run mypy agent

test:
	uv run pytest
