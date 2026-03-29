FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DEFAULT_TIMEOUT=120

COPY requirements.txt .
# Медленный/нестабильный доступ к pypi.org — дольше ждём и больше ретраев
RUN pip install --timeout 40 --retries 3 -r requirements.txt

COPY bot.py .

EXPOSE 8000

CMD ["python", "bot.py"]
