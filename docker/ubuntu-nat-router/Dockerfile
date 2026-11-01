FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive

# iproute2 supplies `tc`, which scripts/network/inject_wan_latency.sh needs for
# the inter-LAN netem emulation.
RUN apt-get update && \
    apt-get install -y iputils-ping net-tools iproute2 && \
    rm -rf /var/lib/apt/lists/*

CMD ["/bin/bash"]
