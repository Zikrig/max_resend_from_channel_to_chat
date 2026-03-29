FROM python:3.12-slim

WORKDIR /app

# Несколько индексов: одно зеркало могло отдать «versions: none» (как Яндекс + httpx).
# Переопределение: --build-arg PIP_INDEX_URL=...
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DEFAULT_TIMEOUT=120

COPY requirements.txt .
RUN pip install \
    --index-url "${PIP_INDEX_URL}" \
    --extra-index-url https://pypi.org/simple \
    --extra-index-url https://mirror.yandex.ru/mirrors/pypi/simple/ \
    --timeout 120 \
    --retries 10 \
    -r requirements.txt

COPY bot.py .

EXPOSE 8000

CMD ["python", "bot.py"]
