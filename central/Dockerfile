FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ARG BUILD_VERSION=dev
COPY central/ central/
RUN echo "${BUILD_VERSION}" > /app/central/VERSION

RUN mkdir -p /app/data

EXPOSE 8080

CMD ["uvicorn", "central.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
