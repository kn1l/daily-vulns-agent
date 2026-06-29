FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    DAILY_VULNS_CONFIG=/app/config.yaml

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && npm install -g @openai/codex \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY prompts ./prompts
COPY skills ./skills
COPY config.example.yaml ./config.example.yaml

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/runs /app/public /app/state \
    && chown -R appuser:appuser /app

EXPOSE 8000

CMD ["sh", "-c", "mkdir -p /app/state/scheduler_runs /app/runs /app/public/reports /app/public/assets && exec uvicorn daily_vulns_agent.web:app --host 0.0.0.0 --port 8000"]
