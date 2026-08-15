FROM python:3.14-alpine3.23@sha256:02da11a8d221ca167aa07de20b3cd7104c1f01227f4b02b1fa13cf6517280a81

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1

WORKDIR /app

RUN apk add --no-cache \
  bash \
  ca-certificates \
  curl \
  libxml2-utils \
  tzdata && \
  python -m pip install --no-cache-dir uv

# Install the exact locked runtime dependencies first to improve layer caching.
COPY pyproject.toml uv.lock README.md buzz.dist.yml /app/
RUN uv export --frozen --no-dev --no-emit-project --output-file /tmp/requirements.txt && \
  uv pip install --system --require-hashes --requirements /tmp/requirements.txt && \
  rm /tmp/requirements.txt

# Copy application code and scripts
COPY buzz /app/buzz
COPY scripts /app/scripts

RUN chmod +x /app/scripts/*.sh

ENTRYPOINT ["python3", "-m", "buzz"]
CMD ["dav"]
