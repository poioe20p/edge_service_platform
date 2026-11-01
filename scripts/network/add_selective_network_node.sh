#!/bin/bash

# Thin wrapper around add_network_node.sh: Tier 1 containers run standalone mongod, so the
# attach steps are identical (the controller's SelectiveStorageNodeAdder calls this entry point).

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /bin/bash "${SCRIPT_DIR}/add_network_node.sh" "$@"
