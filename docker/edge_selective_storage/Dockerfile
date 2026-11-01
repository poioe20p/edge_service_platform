# Tier 1 selective-sync node: standalone mongod (no --replSet) on 27018, the
# inherited mongo_telemetry sidecar reporting STANDALONE_CACHE, and one
# ForwarderWorker per hot collection behind a Flask admin endpoint.
#
# Requires edge_storage_server:latest to be built first.
FROM edge_storage_server:latest

RUN pip3 install --no-cache-dir flask

COPY config.py /config.py
COPY resume_token.py /resume_token.py
COPY telemetry.py /telemetry.py
COPY forwarder.py /forwarder.py
COPY admin.py /admin.py
COPY selective_sync_supervisor.py /selective_sync_supervisor.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# POST /forwarder_config reconfigure endpoint.
EXPOSE 5001

VOLUME ["/var/lib/selective_sync"]

CMD ["/entrypoint.sh"]
