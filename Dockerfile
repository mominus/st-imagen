FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py ./run.py
COPY README.md ./README.md

RUN addgroup --system --gid 10001 app && \
    adduser --system --uid 10001 --ingroup app --home /app app && \
    mkdir -p /app/data/uploads && chown -R app:app /app/data

USER 10001:10001

EXPOSE 8001

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8001 --workers ${UVICORN_WORKERS:-1} --log-level ${UVICORN_LOG_LEVEL:-info}"]
