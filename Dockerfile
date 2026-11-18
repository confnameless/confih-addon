FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app

RUN useradd --create-home --uid 10001 addon

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# qobuz_check.py is useful inside the VPN namespace:
#   docker compose -f docker-compose.vpn.yml exec addon \
#     python scripts/qobuz_check.py --query "daft punk" --quality max
COPY scripts/healthcheck.py scripts/qobuz_check.py ./scripts/

RUN mkdir -p /data && chown -R addon:addon /data /app
USER addon

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "scripts/healthcheck.py"]

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
