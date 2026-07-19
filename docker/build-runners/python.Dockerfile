FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

LABEL org.opencontainers.image.title="Zade Python build runner"
LABEL org.opencontainers.image.description="Network-isolated Python verification runtime for governed local builds"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python -m pip install "pytest==9.1.1" \
    && groupadd --gid 10001 zade \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin zade

USER 10001:10001
WORKDIR /workspace
