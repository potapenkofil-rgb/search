FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

ARG SCREENSHOTS=0
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && if [ "$SCREENSHOTS" = "1" ]; then pip install playwright && playwright install --with-deps chromium; fi

COPY leadhunter ./leadhunter
COPY config.example.yaml ./

VOLUME ["/app/data"]
CMD ["python", "-m", "leadhunter", "run"]
