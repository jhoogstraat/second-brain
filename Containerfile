FROM docker.io/library/python:3.13-slim-trixie

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project
COPY src ./src
RUN uv sync --locked

RUN useradd --uid 1000 --create-home brain && mkdir /data && chown brain /data
USER brain
ENV PATH=/app/.venv/bin:$PATH BRAIN_DIR=/data/brain FASTMCP_HOME=/data/state PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000
CMD ["second-brain"]
