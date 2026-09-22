# Tools live only in the local act runner, never in the application image/host.
FROM docker:28.0.4-cli AS docker
FROM node:22.16.0-bookworm-slim AS node
FROM python:3.11.16-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git bash \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=ghcr.io/astral-sh/uv:0.12.9@sha256:8b940d3a9d65bed080436972241af2e21c84b5e8c9193f7014ed71479ee795ff /uv /usr/local/bin/uv
RUN curl -fsSL "https://github.com/kubernetes-sigs/kind/releases/download/v0.31.0/kind-linux-${TARGETARCH}" -o /usr/local/bin/kind \
    && curl -fsSL "https://github.com/kubernetes-sigs/kind/releases/download/v0.31.0/kind-linux-${TARGETARCH}.sha256sum" -o /tmp/kind.sha256 \
    && cd /usr/local/bin && printf '%s  kind\n' "$(cut -d ' ' -f1 /tmp/kind.sha256)" | sha256sum -c - \
    && curl -fsSL "https://dl.k8s.io/release/v1.32.2/bin/linux/${TARGETARCH}/kubectl" -o /usr/local/bin/kubectl \
    && curl -fsSL "https://dl.k8s.io/release/v1.32.2/bin/linux/${TARGETARCH}/kubectl.sha256" -o /tmp/kubectl.sha256 \
    && printf '%s  kubectl\n' "$(cat /tmp/kubectl.sha256)" | sha256sum -c - \
    && chmod +x /usr/local/bin/kind /usr/local/bin/kubectl \
    && rm /tmp/kind.sha256 /tmp/kubectl.sha256
ENV UV_PYTHON_DOWNLOADS=never
