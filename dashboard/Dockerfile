FROM python:3.12-slim

WORKDIR /app

COPY dashboard/requirements.txt /app/dashboard/requirements.txt
RUN pip install --no-cache-dir -r /app/dashboard/requirements.txt

COPY common /app/common
COPY dashboard /app/dashboard

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8080
# Shell form (not exec-form JSON array) so ${PORT} expands - Render injects
# its own PORT env var and expects the service to listen on it; local
# docker-compose doesn't set PORT, so this falls back to 8080.
CMD uvicorn dashboard.app:app --host 0.0.0.0 --port ${PORT:-8080}
