FROM astral/uv:python3.14-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1

WORKDIR /app

RUN apk add --no-cache \
  bash \
  ca-certificates \
  curl \
  libxml2-utils

# COPY buzz /app/buzz

CMD ["uv", "run", "python3", "-m", "buzz.app"]
