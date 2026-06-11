FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --upgrade pip \
    && pip install --no-cache-dir mypy==1.13.0

COPY src /tmp/src

CMD ["bash"]
