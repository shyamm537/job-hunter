.PHONY: scrape process app test lint

scrape:
	python -m src.ingestion.cli

process:
	python -m src.llm.cli

app:
	streamlit run src/app/main.py

test:
	pytest tests/

lint:
	ruff check src tests
