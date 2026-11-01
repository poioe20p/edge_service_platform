FROM python:3.10-slim-bullseye

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        libxml2-dev \
        libxslt1-dev \
        libffi-dev \
        libssl-dev \
        netcat-openbsd \
        wget \
        ca-certificates \
        gnupg \
    && rm -rf /var/lib/apt/lists/*

# mongos is installed but never invoked; kept so the image matches the built one.
RUN wget -qO /tmp/mongodb-server-7.0.asc https://www.mongodb.org/static/pgp/server-7.0.asc && \
    gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg /tmp/mongodb-server-7.0.asc && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg] https://repo.mongodb.org/apt/debian bullseye/mongodb-org/7.0 main" > /etc/apt/sources.list.d/mongodb-org-7.0.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends mongodb-org-mongos && \
    rm -rf /var/lib/apt/lists/* /tmp/mongodb-server-7.0.asc

# The controller shell-executes source/scripts/network/*.sh from inside this
# container, so docker-ce-cli plus nsenter/ip tooling must live here too.
RUN install -m 0755 -d /etc/apt/keyrings \
    && wget -qO - https://download.docker.com/linux/debian/gpg \
       | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bullseye stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        iproute2 \
        util-linux \
        sudo \
        docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY logging.conf /etc/osken/logging.conf
COPY entrypoint.py /usr/local/bin/osken-entrypoint.py

RUN pip install --upgrade pip setuptools wheel && \
    pip install os-ken==3.1.1 && \
    pip install eventlet python-dotenv pymongo networkx mininet pydantic zmq

ENTRYPOINT ["python3", "/usr/local/bin/osken-entrypoint.py"]
CMD ["--help"]