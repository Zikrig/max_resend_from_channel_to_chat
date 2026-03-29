FROM python:3.12-slim

WORKDIR /app

# Сборка: pip тянет пакеты с этого индекса. При таймаутах до pypi.org переопределите:
#   docker build --build-arg PIP_INDEX_URL=https://mirror.yandex.ru/mirrors/pypi/simple/ .
# Другие зеркала: https://pypi.tuna.tsinghua.edu.cn/simple (CN), официальный: https://pypi.org/simple
ARG PIP_INDEX_URL=https://mirror.yandex.ru/mirrors/pypi/simple/
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DEFAULT_TIMEOUT=120

COPY requirements.txt .
RUN pip install --timeout 120 --retries 10 -r requirements.txt

COPY bot.py .

EXPOSE 8000

CMD ["python", "bot.py"]
