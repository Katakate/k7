# Spec 10b workload: small but realistic. apt + pip generate thousands of
# small files (metadata pressure), git clone is inode/dir-creation heavy,
# ``dd ... conv=fsync`` measures large-block sync write throughput. Each
# layer is bounded so the whole no-cache build completes in a few minutes
# even on the slowest path (k7-ql-r2).
#
# Pinned to ``debian:12-slim`` (no floating tag) so the benchmark is
# reproducible across runs. Image digest is captured in the run log.
FROM debian:12-slim
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential python3 python3-pip python3-venv git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir numpy pandas requests pytest httpx pydantic \
 && find /opt/venv -name '__pycache__' -prune -exec rm -rf {} + \
 && /opt/venv/bin/python -c "import numpy, pandas; print('ok')"
RUN git clone --depth=1 https://github.com/python/cpython.git /tmp/cpython \
 && find /tmp/cpython -type f | wc -l
RUN dd if=/dev/zero of=/tmp/big bs=1M count=256 conv=fsync \
 && rm /tmp/big
CMD ["/bin/true"]
