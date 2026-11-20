FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUTOPILOT_RUNTIME=cloud \
    AUTOPILOT_DATA_DIR=/app/data

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.txt
COPY microtask_monitor /app/microtask_monitor
COPY reports /app/reports
COPY dashboard /app/dashboard
RUN mkdir -p /app/data
EXPOSE 10000
CMD ["python", "-m", "microtask_monitor.cloud_service"]
