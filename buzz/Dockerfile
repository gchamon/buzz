FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
  bash \
  ca-certificates \
  curl \
  libxml2-utils \
  && rm -rf /var/lib/apt/lists/*

# COPY buzz /app/buzz

CMD ["python3", "-m", "buzz.app"]
