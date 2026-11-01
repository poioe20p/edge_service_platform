FROM ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y openvswitch-switch openvswitch-common iproute2 net-tools && \
    mkdir -p /var/run/openvswitch /etc/openvswitch && \
    rm -rf /var/lib/apt/lists/*

COPY start.sh ./start.sh
RUN chmod +x ./start.sh

CMD ["./start.sh"]
