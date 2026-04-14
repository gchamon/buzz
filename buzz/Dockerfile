FROM astral/uv:python3.14-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1

WORKDIR /app

RUN apk add --no-cache \
  bash \
  ca-certificates \
  curl \
  libxml2-utils

COPY buzz /app/buzz
COPY scripts /app/scripts
COPY pyproject.toml /app/

RUN uv pip install --system .
RUN chmod +x /app/scripts/*.sh

ENTRYPOINT ["python3", "-m", "buzz"]
CMD ["dav"]
