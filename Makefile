.PHONY: venv install seed samples run test docker

venv:
	python3 -m venv .venv

install: venv
	.venv/bin/pip install -U pip
	.venv/bin/pip install -r requirements.txt

seed:
	.venv/bin/python -m app.seed

samples:
	.venv/bin/python samples/make_samples.py

run:
	.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8080

test:
	.venv/bin/python tests/test_system.py

docker:
	docker compose up -d --build
