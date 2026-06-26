FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    DAILY_VULNS_CONFIG=/app/config.yaml

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY prompts ./prompts
COPY skills ./skills
COPY config.example.yaml ./config.example.yaml

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/runs /app/public /app/state \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "daily_vulns_agent.web:app", "--host", "0.0.0.0", "--port", "8000"]
