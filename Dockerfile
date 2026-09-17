FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        openssh-client \
        fuse-overlayfs \
        rsync \
        iptables \
        docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir .

EXPOSE 8000

ENTRYPOINT ["oduflow"]
CMD ["--transport", "http"]
