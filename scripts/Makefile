
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
FAULT_PLAN    ?=
OSKEN_ENV_FILE ?=
OSKEN_ENV_OVERRIDE_FILE ?=
PROBE_CAPTURE_RUN_DIR ?=
PROBE_CAPTURE_ROOT    ?=


.PHONY: setup_network \
        create_clients teardown_clients \
	setup_test_data seed_content_items seed_user_profiles create_indexes export_snapshot \
	run_experiment cleanup_metrics probe_capture_preflight probe_capture_launch \
	install_commit_msg_hook uninstall_commit_msg_hook preview_commit_msg


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
	  --data-seed $(DATA_SEED)

seed_user_profiles:
	$(PYTHON) testing/seed_user_profiles.py \
	  --mongo-lan1 $(MONGO_LAN1) \
	  --mongo-lan2 $(MONGO_LAN2) \
	  --content-items $(CONTENT_ITEMS) \
	  --users $(USERS) \
	  --data-seed $(DATA_SEED)

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
	  $(if $(strip $(FAULT_PLAN)),--fault-plan "$(strip $(FAULT_PLAN))") \
	  $(if $(filter-out 0 false,$(SKIP_CLIENTS)),--skip-clients) \
	  $(if $(filter-out 0 false,$(SKIP_SEED)),--skip-seed) \
	  $(if $(filter-out 0 false,$(SKIP_SNAPSHOT)),--skip-snapshot) \
	  $(if $(filter-out 0 false,$(DRY_RUN)),--dry-run)

## Hard gate; requires aiohttp.
driver_selftest:
	$(PYTHON) testing/openloop_p1_01_driver_selftest.py

## Hard gate before RQ1 v2 blocks.
rq1_sampled_push_selftest:
	$(PYTHON) testing/rq1v2_p2_01_sampled_push_selftest.py

## Hard gate before RQ1 v2 blocks.
rq1_analyzer_selftest:
	$(PYTHON) testing/rq1v2_p1_01_analyzer_selftest.py

## Hard gate before RQ3 v2 blocks.
rq3_analyzer_selftest:
	$(PYTHON) testing/rq3v2_p1_01_analyzer_selftest.py

## Hard gate before RQ3 v2 blocks.
rq3_app_ready_selftest:
	$(PYTHON) testing/rq3v2_p2_01_app_ready_selftest.py

## RQ3 v2 Phase-6 pre-flight gates (VM-side; run on cloud-vm-rq3).
## See docs/operation/testing/experiment/v2/rq3/rq3_preflight.md.
rq3_preflight:
	bash testing/rq3v2_p6_01_preflight.sh

probe_capture_preflight:
	@set -eu; \
	run_dir="$(strip $(PROBE_CAPTURE_RUN_DIR))"; \
	capture_root="$(strip $(PROBE_CAPTURE_ROOT))"; \
	if [ -z "$$capture_root" ]; then \
		echo "ERROR: PROBE_CAPTURE_ROOT is required" >&2; \
		exit 1; \
	fi; \
	if [ -n "$$run_dir" ]; then \
		bash testing/capture_reverse_hotspot_probe.sh --preflight --run-dir "$$run_dir" --capture-root "$$capture_root"; \
	else \
		bash testing/capture_reverse_hotspot_probe.sh --preflight --capture-root "$$capture_root"; \
	fi

probe_capture_launch:
	@set -eu; \
	run_dir="$(strip $(PROBE_CAPTURE_RUN_DIR))"; \
	capture_root="$(strip $(PROBE_CAPTURE_ROOT))"; \
	if [ -z "$$run_dir" ]; then \
		echo "ERROR: PROBE_CAPTURE_RUN_DIR is required" >&2; \
		exit 1; \
	fi; \
	if [ -z "$$capture_root" ]; then \
		echo "ERROR: PROBE_CAPTURE_ROOT is required" >&2; \
		exit 1; \
	fi; \
	mkdir -p "$$capture_root"; \
	nohup bash testing/capture_reverse_hotspot_probe.sh --run-dir "$$run_dir" --capture-root "$$capture_root" > "$$capture_root/helper.log" 2>&1 < /dev/null & \
	helper_pid=$$!; \
	echo "HELPER_PID=$$helper_pid"; \
	echo "HELPER_LOG=$$capture_root/helper.log"

install_commit_msg_hook:
	bash tools/install_commit_msg_hook.sh install

uninstall_commit_msg_hook:
	bash tools/install_commit_msg_hook.sh uninstall

preview_commit_msg:
	$(PYTHON) tools/generate_commit_message.py --from-staged
