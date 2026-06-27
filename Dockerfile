FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_NO_CACHE_DIR=1 \
    ARGUS_RUN_ROOT=/var/lib/argus \
    ARGUS_CONFIG=/config/argus.yaml \
    ARGUS_CONFIG_V2=/config/argus.yaml

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install --upgrade pip \
    && python -m pip install . \
    && useradd --create-home --system --home-dir /home/argus argus \
    && mkdir -p /config /var/lib/argus \
    && chown -R argus:argus /config /var/lib/argus

USER argus

VOLUME ["/config", "/var/lib/argus"]

ENTRYPOINT ["argus"]
CMD ["--help"]
