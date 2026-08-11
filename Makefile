.PHONY: setup scrape process app test lint

# venv binary dir differs by OS (Windows uses Scripts/, POSIX uses bin/)
ifeq ($(OS),Windows_NT)
    VENV_BIN := venv/Scripts
else
    VENV_BIN := venv/bin
endif

# One-time installer. Run once after cloning: creates the virtualenv and
# installs dependencies into it. It canNOT activate the venv for your shell
# (a child process can't) — activate it yourself afterwards, as printed.
setup:
	python -m venv venv
	$(VENV_BIN)/python -m pip install --upgrade pip
	$(VENV_BIN)/python -m pip install -r requirements.txt
	@test -f config.yaml || cp config.yaml.example config.yaml
	@echo ""
	@echo "Setup complete. Next:"
	@echo "  1. Activate:        source $(VENV_BIN)/activate   (Windows: $(VENV_BIN)\\activate)"
	@echo "  2. Edit config.yaml (search terms, llm.model, resume_summary)"
	@echo "  3. make scrape && make process && make app"

scrape:
	python -m src.ingestion.cli

process:
	python -m src.llm.cli

app:
	streamlit run src/app/main.py --server.address localhost

test:
	pytest tests/

lint:
	ruff check src tests
