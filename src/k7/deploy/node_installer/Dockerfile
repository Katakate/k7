# k7-node-installer: roles/k7_node_prep + pinned ansible-core +
# every release artifact the role would otherwise download (artifacts.txt).
# Build context is the repo root:
#   docker build -f src/k7/deploy/node_installer/Dockerfile -t k7-node-installer:<k7-version> .
# Both stages share one base; literal FROM lines so Dependabot can bump the digest.
FROM python:3.15.0-slim-bookworm@sha256:0e019bbdfb5d9874cb2389f1c2884b64aa2ac97c99960b5bc521cef978c0617d AS artifacts
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY src/k7/deploy/node_installer/artifacts.txt src/k7/deploy/node_installer/fetch-artifacts.sh /tmp/
RUN bash /tmp/fetch-artifacts.sh /tmp/artifacts.txt /opt/k7-artifacts

# No curl in the runtime image: the installer cannot download.
FROM python:3.15.0-slim-bookworm@sha256:0e019bbdfb5d9874cb2389f1c2884b64aa2ac97c99960b5bc521cef978c0617d
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
