VENV := .venv

$(VENV)/bin/activate:
	python3 -m venv $(VENV)

run: $(VENV)/bin/activate
	@. $(VENV)/bin/activate && python main.py

nvim: $(VENV)/bin/activate
	@. $(VENV)/bin/activate && nvim .
