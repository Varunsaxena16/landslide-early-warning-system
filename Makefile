# Sikkim Landslide Platform
.DEFAULT_GOAL := help
PY      := .venv/bin/python
PIP     := .venv/bin/pip
PORT    ?= 8000
IMAGE   ?= sikkim-landslide
REGION  ?= asia-south1

.PHONY: help venv install data data-force serve dev test lint clean docker docker-run deploy check

help:  ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	 | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv:  ## create the virtualenv
	python3 -m venv .venv && $(PIP) install --upgrade pip

install: venv  ## install all dependencies
	$(PIP) install -r requirements-dev.txt

data:  ## build data/processed from the raw delivery (skips if already built)
	@if [ -f data/processed/stats.json ]; then \
	  echo "data/processed already built — use 'make data-force' to rebuild"; \
	else $(PY) -m pipeline.run_all; fi

data-force:  ## rebuild every pipeline step from scratch
	$(PY) -m pipeline.run_all

inventory:  ## report on the raw delivery without processing it
	$(PY) -m pipeline.run_all --only 1

exposure:  ## fetch WorldPop + Open Buildings and rebuild the exposure COGs
	$(PY) tools/fetch_exposure.py

alerts:  ## refresh Open-Meteo rain/soil alerts on published hotspots
	$(PY) tools/fetch_alerts.py --force

surfaces:  ## rain, soil moisture and population drapes for early warning
	$(PY) tools/fetch_surfaces.py --force

audit:  ## grid, population and forecast credibility report
	$(PY) scripts/run_credibility_audit.py

serve:  ## run the dashboard at http://localhost:$(PORT)
	$(PY) -m uvicorn backend.app:app --host 0.0.0.0 --port $(PORT)

dev:  ## run with auto-reload
	$(PY) -m uvicorn backend.app:app --reload --port $(PORT)

test:  ## run the test suite
	$(PY) -m pytest misc/tests/ -q

check: test  ## everything CI would run
	@$(PY) -c "import json;d=json.load(open('data/processed/stats.json'));\
	print('lift %.2fx  AUC %.3f' % (d['validation']['headline']['lift'], d['validation']['headline']['auc']))"

docker:  ## build the container image
	docker build -t $(IMAGE) .

docker-run: docker  ## build and run the container on :8080
	docker run --rm -p 8080:8080 -e PORT=8080 $(IMAGE)

deploy:  ## deploy to Cloud Run (needs PROJECT=<gcp-project-id>)
	@test -n "$(PROJECT)" || (echo "usage: make deploy PROJECT=<gcp-project-id>"; exit 1)
	gcloud builds submit --tag gcr.io/$(PROJECT)/$(IMAGE) --project $(PROJECT)
	gcloud run deploy $(IMAGE) \
	  --image gcr.io/$(PROJECT)/$(IMAGE) \
	  --project $(PROJECT) --region $(REGION) \
	  --platform managed --allow-unauthenticated \
	  --memory 1Gi --cpu 1 --concurrency 40 --timeout 60 --min-instances 0

clean:  ## remove processed data (raw delivery untouched)
	rm -rf data/processed
