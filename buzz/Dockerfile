FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY buzz /app/buzz

CMD ["python3", "-m", "buzz.app"]
