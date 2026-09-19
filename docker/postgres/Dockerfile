ARG POSTGRES_BASE=postgres:15-bookworm@sha256:539ceaaae49b3a7c8a04467cf00cc6788d8e3f1675df41860d86eebc4c40524f
FROM ${POSTGRES_BASE}

ARG POSTGRES_IMAGE_VERSION=15-bookworm-1
LABEL org.opencontainers.image.title="Oduflow production PostgreSQL" \
      org.opencontainers.image.version="${POSTGRES_IMAGE_VERSION}"

# Install at build time; recovery must never need apt or free disk for packages.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && update-ca-certificates \
 && test -s /etc/ssl/certs/ca-certificates.crt \
 && rm -rf /var/lib/apt/lists/*
