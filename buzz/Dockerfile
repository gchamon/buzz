FROM python:3.14-alpine3.23@sha256:218761489de417a6eb0808e264cbdd7043ec6659fe5a61898815e9848536541d

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1

WORKDIR /app

# Bake the full CI commit hash into the image for deployment identity.
ARG BUZZ_GIT_SHA=unknown
ENV BUZZ_GIT_SHA=$BUZZ_GIT_SHA

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
  rm /tmp/requirements.txt && \
  python -m pip uninstall --yes uv setuptools pip

# Copy application code and scripts
COPY buzz /app/buzz
COPY scripts /app/scripts

RUN chmod +x /app/scripts/*.sh

ENTRYPOINT ["python3", "-m", "buzz"]
CMD ["dav"]
