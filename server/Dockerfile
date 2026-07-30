FROM python:3.12.10-slim@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system intdemo && useradd --system --gid intdemo --home /app intdemo
WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install --index-url "${PIP_INDEX_URL}" --upgrade pip \
    && pip install --index-url "${PIP_INDEX_URL}" .
COPY alembic.ini ./
COPY alembic ./alembic
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN sed -i 's/\r$//' ./docker-entrypoint.sh \
    && chmod 0755 ./docker-entrypoint.sh

USER root
EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
