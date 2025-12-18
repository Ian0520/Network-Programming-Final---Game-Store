.PHONY: start stop restart reset logs status

PYTHON ?= python3

start:
	@PYTHONPATH=".." PYTHON="$(PYTHON)" ./scripts/start_all.sh

stop:
	@./scripts/stop_all.sh

restart: stop start

reset:
	@./scripts/reset_demo.sh --yes

logs:
	@echo "Logs: ./.run/logs/"
	@ls -1 ./.run/logs 2>/dev/null || true

status:
	@for name in db developer lobby; do \
		if [ -f ./.run/$$name.pid ]; then \
			pid=$$(cat ./.run/$$name.pid); \
			if kill -0 $$pid >/dev/null 2>&1; then echo "$$name running pid=$$pid"; else echo "$$name not running (stale pidfile)"; fi; \
		else \
			echo "$$name not running"; \
		fi; \
	done
