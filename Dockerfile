FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sepko ./sepko
COPY scripts ./scripts
COPY deploy ./deploy

ENV PYTHONUNBUFFERED=1 \
    SEPKO_ENV=production \
    GUNICORN_BIND=0.0.0.0:8000

EXPOSE 8000

CMD ["gunicorn", "-c", "deploy/gunicorn.conf.py", "sepko.main:app"]
