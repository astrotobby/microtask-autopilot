FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUTOPILOT_RUNTIME=cloud \
    AUTOPILOT_DATA_DIR=/app/data \
    AUTOPILOT_PROFILE_PATH=/app/data/profile.json \
    HOME=/home/autopilot \
    PATH=/home/autopilot/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates bubblewrap git ripgrep util-linux \
    && curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh \
    && if [ -x /home/autopilot/.local/bin/codex ]; then ln -sf /home/autopilot/.local/bin/codex /usr/local/bin/codex; fi \
    && command -v codex \
    && rm -rf /var/lib/apt/lists/*
COPY microtask_monitor /app/microtask_monitor
COPY reports /app/reports
COPY dashboard /app/dashboard
COPY docker/cloud-entrypoint.sh /usr/local/bin/cloud-entrypoint
RUN groupadd --gid 10001 autopilot \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin autopilot \
    && chmod 0755 /usr/local/bin/cloud-entrypoint \
    && mkdir -p /app/data \
    && chown -R 10001:10001 /app/data /home/autopilot
EXPOSE 10000
ENTRYPOINT ["/usr/local/bin/cloud-entrypoint"]
CMD ["python", "-m", "microtask_monitor.cloud_service"]
