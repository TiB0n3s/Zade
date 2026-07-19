FROM node:22-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3

LABEL org.opencontainers.image.title="Zade Node build runner"
LABEL org.opencontainers.image.description="Network-isolated Node verification runtime for governed local builds"

ENV NODE_ENV=test \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_UPDATE_NOTIFIER=false

USER node
WORKDIR /workspace
