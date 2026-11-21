FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        openssh-client \
        fuse-overlayfs \
        rsync \
        iptables \
        docker.io \
        docker-cli \
        tini \
    && rm -rf /var/lib/apt/lists/*

# BuildKit for the image build tools (start_image_build, Stack `build:`):
# docker-cli above plus its buildx plugin. Without them Oduflow falls back to
# the Docker Engine's legacy builder, which cannot run Dockerfiles that use
# $BUILDPLATFORM, RUN --mount or COPY --link.
COPY --from=docker/buildx-bin:0.38.2 /buildx /usr/local/lib/docker/cli-plugins/docker-buildx

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir .

EXPOSE 8000

# tini runs as PID 1 and reaps orphaned children: fuse-overlayfs and detached
# git maintenance daemonize, and oduflow never waits for them itself.
ENTRYPOINT ["tini", "--", "oduflow"]
CMD ["--transport", "http"]
