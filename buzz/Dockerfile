FROM astral/uv:python3.14-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1

WORKDIR /app

RUN apk add --no-cache \
  bash \
  ca-certificates \
  curl \
  libxml2-utils

# Install dependencies first to improve Docker layer caching
COPY pyproject.toml README.md buzz.dist.yml /app/
RUN mkdir -p /app/buzz/core && touch /app/buzz/__init__.py /app/buzz/core/__init__.py && \
    uv pip install --system .

# Copy application code and scripts
COPY buzz /app/buzz
COPY scripts /app/scripts

RUN chmod +x /app/scripts/*.sh

ENTRYPOINT ["python3", "-m", "buzz"]
CMD ["dav"]
