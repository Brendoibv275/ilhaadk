FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY sdr_ilha_ar ./sdr_ilha_ar
COPY agent.py ./agent.py

RUN pip install --no-cache-dir -e .

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "sdr_ilha_ar.webhook_api:app", "--host", "0.0.0.0", "--port", "8000"]
