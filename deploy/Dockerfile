# Exact release tag verified against Docker Official Images, 2026-10-06.
# Immutable multi-platform indices pinned; platform child manifests are saved in image-candidates.json.
FROM node:24.21.0-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS node_runtime
FROM python:3.12.15-slim-bookworm@sha256:7753c33391fc9f01d1984375bf375eb6686d52ba10db6043a86634a5ccf90dcf AS runtime
COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 openssl \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/gateway PYTHONPATH=/app LITELLM_TELEMETRY=False DO_NOT_TRACK=1 \
    PRISMA_BINARY_CACHE_DIR=/opt/prisma PRISMA_NODEENV_CACHE_DIR=/opt/prisma-nodeenv \
    PRISMA_USE_GLOBAL_NODE=true PRISMA_VERSION=5.17.0 \
    PRISMA_EXPECTED_ENGINE_VERSION=393aa359c9ad4a4bb28630fb5613f9c281cde053
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN mkdir -p /opt/prisma /opt/prisma-nodeenv \
    && pip install --no-cache-dir --require-hashes -r /app/requirements.lock \
    && python -m prisma generate --schema /usr/local/lib/python3.12/site-packages/litellm/proxy/schema.prisma \
    && groupadd --gid 10001 gateway \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/gateway gateway \
    && mkdir -p /app/.runtime /var/lib/gateway \
    && chown -R gateway:gateway /app/.runtime /var/lib/gateway /home/gateway \
    && chmod -R a+rX /opt/prisma /opt/prisma-nodeenv
COPY gateway /app/gateway
COPY deploy/reviewed_start.py deploy/privilege_drop.py deploy/probe_egress.py /app/deploy/
USER 10001:10001
EXPOSE 4000
CMD ["python", "-m", "gateway.bootstrap", "--config", "/app/config/policy.yaml", "--profile", "production", "--port", "4000"]

FROM runtime AS test
COPY tests /app/tests
