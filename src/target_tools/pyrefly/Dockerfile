FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Astral's uv (so we can pull a pinned pyrefly without a separate Rust toolchain).
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && /root/.local/bin/uv tool install pyrefly
ENV PATH="/root/.local/bin:/root/.local/share/uv/tools/pyrefly/bin:${PATH}"

COPY src /tmp/src

CMD ["bash"]
