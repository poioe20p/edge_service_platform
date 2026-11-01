FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    MONGO_VERSION=7.0 \
    AGGREGATOR_PULL_ADDR= \
    LAN_ID= \
    MONGO_URI= \
    MONGO_PORT=27018 \
    TELEMETRY_INTERVAL_S=10 \
    MONGO_REPLSET=

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl gnupg iputils-ping iproute2 net-tools iperf3 \
       python3 python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://pgp.mongodb.com/server-${MONGO_VERSION}.asc | gpg --dearmor -o /etc/apt/keyrings/mongodb-server-${MONGO_VERSION}.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/mongodb-server-${MONGO_VERSION}.gpg arch=$(dpkg --print-architecture)] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/${MONGO_VERSION} multiverse" \
         > /etc/apt/sources.list.d/mongodb-org-${MONGO_VERSION}.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends mongodb-org \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir pyzmq psutil pymongo

RUN mkdir -p /data/db \
    && chown -R root:root /data/db

COPY mongo_telemetry.py /mongo_telemetry.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/data/db"]

# Documentation only; the 27018 wiring is done by scripts/network.
EXPOSE 27018

CMD ["/entrypoint.sh"]
