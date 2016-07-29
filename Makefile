dir = $(shell pwd)
VENV_DIR = .venv
VENV_RUN = . $(VENV_DIR)/bin/activate

usage:             ## Show this help
	@fgrep -h "##" $(MAKEFILE_LIST) | fgrep -v fgrep | sed -e 's/\\$$//' | sed -e 's/##//'

build:             ## Install local pip and npm dependencies
	(test `which virtualenv` || pip install virtualenv || sudo pip install virtualenv)
	(test -e $(VENV_DIR) || virtualenv $(VENV_DIR))
	# due to a bug in scipy, numpy needs to be installed first:
	($(VENV_RUN) && pip install numpy)
	($(VENV_RUN) && pip install -r requirements.txt)
	make npm

install-prereq:    ## Install prerequisites via apt-get or yum (if available)
	which apt-get && sudo apt-get -y install libblas-dev liblapack-dev
	which yum && sudo yum -y install blas-devel lapack-devel numpy-f2py

npm:               ## Install node.js/npm dependencies
	cd $(dir)/web/ && npm install

test:              ## Run tests
	($(VENV_RUN) && PYTHONPATH=$(dir)/test nosetests --with-coverage --with-xunit --cover-package=themis test/) && \
	make lint

lint:              ## Run code linter to check code style
	($(VENV_RUN); pep8 --max-line-length=120 --ignore=E128 --exclude=web,bin,$(VENV_DIR) .)

server:            ## Start the server on port 8081
	($(VENV_RUN) && eval `ssh-agent -s` && PYTHONPATH=$(dir)/src src/themis/main.py server_and_loop --port=8082 --log=themis.log)

.PHONY: build test
