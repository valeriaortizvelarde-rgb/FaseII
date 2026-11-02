FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY pyproject.toml poetry.lock README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY resources/data ./resources/data
COPY resources/static ./resources/static

EXPOSE 8000

CMD ["uvicorn", "infrastructure.entrypoint.fastapi_app:app", "--host", "0.0.0.0", "--port", "8000"]
