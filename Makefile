CODE_DIRS = src/ tests/
ISORT_PARAMS = --ignore-whitespace --settings-path . --recursive $(CODE_DIRS)

all: lint mypy

lint: mypy
	flake8 $(CODE_DIRS)
	isort $(ISORT_PARAMS) --diff --check-only

mypy:
	mypy --ignore-missing-imports --check-untyped-defs $(CODE_DIRS)
	mypy --ignore-missing-imports --disallow-untyped-defs src/monitoring_service

isort:
	isort $(ISORT_PARAMS)

test:
	py.test -v tests
