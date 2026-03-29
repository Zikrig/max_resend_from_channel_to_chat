FROM python:3.12-slim

WORKDIR /app

# UNEXPECTED_EOF / SSLEOF при pip — обрыв TLS (DPI, фильтры, «рвущийся» канал), не версия пакета.
# Обход: (1) сборка на Linux: docker build --network=host .
# (2) без сети в образе: скачать колёса под Linux и положить в ./wheels/
#     docker run --rm -v "$PWD/wheels:/w" -v "$PWD/requirements.txt:/r.txt" python:3.12-slim \
#       bash -c "pip download -r /r.txt -d /w"
#     затем docker compose build (ветка «offline» сработает, если в wheels/*.whl есть файлы).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && update-ca-certificates

# Несколько индексов; основной можно заменить: --build-arg PIP_INDEX_URL=...
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DEFAULT_TIMEOUT=300

COPY requirements.txt .
COPY wheels ./wheels/

RUN sh -c 'if ls wheels/*.whl 1> /dev/null 2>&1; then \
       echo "Installing from local wheels/ (offline)"; \
       pip install --no-cache-dir --no-index --find-links wheels -r requirements.txt; \
     else \
       echo "Installing from indexes (network)"; \
       pip install --no-cache-dir \
         --index-url "${PIP_INDEX_URL}" \
         --extra-index-url https://pypi.org/simple \
         --extra-index-url https://mirror.yandex.ru/mirrors/pypi/simple/ \
         --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple/ \
         --timeout 300 \
         --retries 20 \
         -r requirements.txt; \
     fi'

COPY bot.py .

EXPOSE 8000

CMD ["python", "bot.py"]
