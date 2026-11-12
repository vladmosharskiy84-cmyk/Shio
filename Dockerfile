# Shio: движок funding-арбитража (виртуальная торговля 24/7).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

# Непривилегированный пользователь; uid 1000 должен иметь право записи в ./data на сервере
RUN groupadd -g 1000 shio && useradd -u 1000 -g 1000 -m -s /usr/sbin/nologin shio

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY engine/ engine/
COPY scanner/ scanner/
# config.yaml в образе — запасной: compose монтирует поверх него config.yaml и config.local.yaml с сервера
COPY config.yaml main.py ./
RUN mkdir -p /app/data/backups && chown -R shio:shio /app/data

USER shio

STOPSIGNAL SIGTERM
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s CMD python -m engine health

CMD ["python", "-m", "engine", "run"]
