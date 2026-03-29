# --- builder: здесь pip; можно направить трафик через HTTP(S)-прокси на хосте (см. docker-compose) ---
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && update-ca-certificates

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DEFAULT_TIMEOUT=300 \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    NO_PROXY=${NO_PROXY}

COPY requirements.txt .
RUN pip install \
    --index-url "${PIP_INDEX_URL}" \
    --extra-index-url https://pypi.org/simple \
    --extra-index-url https://mirror.yandex.ru/mirrors/pypi/simple/ \
    --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple/ \
    --timeout 30 \
    --retries 4 \
    -r requirements.txt

# --- финальный образ без HTTP_PROXY (бот ходит к MAX API напрямую) ---
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && update-ca-certificates

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

COPY bot.py .

EXPOSE 8000

CMD ["python", "bot.py"]
