# Pin both upstream images by digest so repeated builds use the same inputs.
FROM python:3.11.16-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b AS python

FROM python AS dependencies
COPY --from=ghcr.io/astral-sh/uv:0.12.9@sha256:8b940d3a9d65bed080436972241af2e21c84b5e8c9193f7014ed71479ee795ff /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache --python /usr/local/bin/python

FROM python AS runtime
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RELAY_DATABASE_URL=sqlite:////data/agent-relay.db
WORKDIR /app
RUN groupadd --gid 10001 relay \
    && useradd --uid 10001 --gid relay --no-create-home --shell /usr/sbin/nologin relay \
    && install -d -o relay -g relay /data
COPY --from=dependencies /app/.venv /app/.venv
COPY main.py database.py storage.py schemas.py errors.py worker.py dashboard.py dashboard.html ./
USER relay:relay
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3).read()"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
