FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY sdr_ilha_ar ./sdr_ilha_ar
COPY agent.py ./agent.py
COPY db ./db

RUN pip install --no-cache-dir -e .

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "sdr_ilha_ar.webhook_api:app", "--host", "0.0.0.0", "--port", "8000"]
