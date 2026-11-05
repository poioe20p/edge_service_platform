
PYTHON       ?= python3
MONGO_LAN1   ?= mongodb://10.0.0.4:27018/
MONGO_LAN2   ?= mongodb://10.0.1.4:27018/
CONTENT_ITEMS ?= 100
USERS         ?= 45
CLIENTS      ?= 3
CLIENT_TCP_SYN_RETRIES ?=
SNAPSHOT_DIR ?= testing/data/workload_snapshot
SKIP_CLIENTS  ?= 1
SKIP_SEED     ?= 1
SKIP_SNAPSHOT ?= 1
DRY_RUN       ?=
RUN_LABEL     ?=
PHASES_CONFIG ?=
DATA_SEED     ?=
TRAFFIC_DRIVER_MODE ?= open_loop
OSKEN_ENV_FILE ?=
OSKEN_ENV_OVERRIDE_FILE ?=


.PHONY: setup_network \
        create_clients teardown_clients \
	setup_test_data seed_content_items seed_user_profiles create_indexes export_snapshot \
	run_experiment cleanup_metrics


setup_network:
	OSKEN_ENV_FILE="$(strip $(OSKEN_ENV_FILE))" \
	OSKEN_ENV_OVERRIDE_FILE="$(strip $(OSKEN_ENV_OVERRIDE_FILE))" \
	bash build_network_setup.sh


create_clients:
	bash network/clients/create_test_clients.sh \
	  --lan 1 --count $(CLIENTS) --prefix lan1_client \
	  $(if $(CLIENT_TCP_SYN_RETRIES),--syn-retries $(CLIENT_TCP_SYN_RETRIES))
	bash network/clients/create_test_clients.sh \
	  --lan 2 --count $(CLIENTS) --prefix lan2_client \
	  $(if $(CLIENT_TCP_SYN_RETRIES),--syn-retries $(CLIENT_TCP_SYN_RETRIES))

teardown_clients:
	bash network/clients/remove_test_clients.sh \
	  --lan 1 --prefix lan1_client
	bash network/clients/remove_test_clients.sh \
	  --lan 2 --prefix lan2_client
	bash cleanup.sh -v


setup_test_data: seed_content_items seed_user_profiles create_indexes export_snapshot

seed_content_items:
	$(PYTHON) testing/seed_content_items.py \
	  --mongo-lan1 $(MONGO_LAN1) \
	  --mongo-lan2 $(MONGO_LAN2) \
	  --content-items $(CONTENT_ITEMS) \
	  $(if $(strip $(DATA_SEED)),--data-seed $(DATA_SEED))

seed_user_profiles:
	$(PYTHON) testing/seed_user_profiles.py \
	  --mongo-lan1 $(MONGO_LAN1) \
	  --mongo-lan2 $(MONGO_LAN2) \
	  --content-items $(CONTENT_ITEMS) \
	  --users $(USERS) \
	  $(if $(strip $(DATA_SEED)),--data-seed $(DATA_SEED))

create_indexes:
	$(PYTHON) testing/create_indexes.py \
	  --mongo-lan1 $(MONGO_LAN1) \
	  --mongo-lan2 $(MONGO_LAN2)

export_snapshot:
	$(PYTHON) testing/export_workload_snapshot.py \
	  --mongo-lan1 $(MONGO_LAN1) \
	  --mongo-lan2 $(MONGO_LAN2) \
	  --output-dir $(SNAPSHOT_DIR)


cleanup_metrics:
	@set -eu; \
	metrics_dir="$(abspath testing/metrics)"; \
	find "$$metrics_dir" -mindepth 1 -maxdepth 1 -type d | while IFS= read -r run_dir; do \
		case "$$run_dir" in \
			"$$metrics_dir"/*) \
				printf 'Removing %s\n' "$$run_dir"; \
				rm -rf -- "$$run_dir"; \
				;; \
			*) \
				echo "Refusing unexpected path: $$run_dir" >&2; \
				exit 1; \
				;; \
		esac; \
	done

run_experiment:
	OSKEN_ENV_FILE="$(strip $(OSKEN_ENV_FILE))" \
	OSKEN_ENV_OVERRIDE_FILE="$(strip $(OSKEN_ENV_OVERRIDE_FILE))" \
	CURL_MAX_TIME="$(CURL_MAX_TIME)" \
	TRAFFIC_DRIVER_MODE="$(strip $(TRAFFIC_DRIVER_MODE))" \
	INFLIGHT_WINDOW="$(strip $(INFLIGHT_WINDOW))" \
	DRAIN_S="$(strip $(DRAIN_S))" \
	sudo -E bash testing/run_experiment.sh \
	  $(if $(strip $(RUN_LABEL)),--run-label "$(strip $(RUN_LABEL))") \
	  --clients-per-lan "$(strip $(CLIENTS))" \
	  --seed-content-items "$(strip $(CONTENT_ITEMS))" \
	  --seed-users "$(strip $(USERS))" \
	  --snapshot-dir $(abspath $(SNAPSHOT_DIR)) \
	  $(if $(strip $(PHASES_CONFIG)),--phases-config "$(strip $(PHASES_CONFIG))") \
	  $(if $(filter-out 0 false,$(SKIP_CLIENTS)),--skip-clients) \
	  $(if $(filter-out 0 false,$(SKIP_SEED)),--skip-seed) \
	  $(if $(filter-out 0 false,$(SKIP_SNAPSHOT)),--skip-snapshot) \
	  $(if $(filter-out 0 false,$(DRY_RUN)),--dry-run)

