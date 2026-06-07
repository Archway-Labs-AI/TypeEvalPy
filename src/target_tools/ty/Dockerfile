FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Astral's uv (pulls ty as needed) + a stable cached install of ty so the
# benchmark run doesn't re-download it 153× across snippets.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && /root/.local/bin/uv tool install ty
ENV PATH="/root/.cargo/bin:/root/.local/bin:/root/.local/share/uv/tools/ty/bin:${PATH}"

COPY src /tmp/src

CMD ["bash"]
