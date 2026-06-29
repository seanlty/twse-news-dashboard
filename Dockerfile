FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    TWSE_DASHBOARD_DATA_ROOT=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && mkdir -p /data/raw

COPY . .

EXPOSE 8000

CMD ["python", "/app/src/main.py", "serve"]
