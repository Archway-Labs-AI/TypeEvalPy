FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Node 20 LTS via NodeSource — minimal, no nvm bloat.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Pinned pyright. Bump deliberately so result deltas can be attributed to
# either the tool or the benchmark, not both at once.
RUN npm install --global pyright@1.1.410

COPY src /tmp/src

CMD ["bash"]
