# k7-node-installer: roles/k7_node_prep + pinned ansible-core +
# every release artifact the role would otherwise download (artifacts.txt).
# Build context is the repo root:
#   docker build -f src/k7/deploy/node_installer/Dockerfile -t k7-node-installer:<k7-version> .
# Both stages share one base; literal FROM lines so Dependabot can bump the digest.
FROM python:3.15.2-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9 AS artifacts
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY src/k7/deploy/node_installer/artifacts.txt src/k7/deploy/node_installer/fetch-artifacts.sh /tmp/
RUN bash /tmp/fetch-artifacts.sh /tmp/artifacts.txt /opt/k7-artifacts

# No curl in the runtime image: the installer cannot download.
FROM python:3.15.2-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9
# file: the role's ELF check is a controller-side lookup. nsenter ships in the base (util-linux).
COPY src/k7/deploy/node_installer/requirements.txt /tmp/requirements.txt
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends file \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --no-deps -r /tmp/requirements.txt \
    && pip check
COPY --from=artifacts /opt/k7-artifacts /opt/k7-artifacts
COPY src/k7/__init__.py /opt/k7/src/k7/__init__.py
COPY src/k7/deploy /opt/k7/src/k7/deploy
WORKDIR /opt/k7/src
ENTRYPOINT ["python3", "-m", "k7.deploy.node_installer.entrypoint"]
