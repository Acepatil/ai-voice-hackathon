FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 GCX_DB=/app/deploy/demo.sqlite GCX_OUT=/tmp/out
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gcx ./gcx
COPY config.yaml ./
COPY deploy/demo.sqlite ./deploy/demo.sqlite
CMD ["sh", "-c", "uvicorn gcx.server:app --host 0.0.0.0 --port ${PORT:-8088}"]
