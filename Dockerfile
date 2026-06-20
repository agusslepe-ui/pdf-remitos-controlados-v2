FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UPLOADS_DIR=/app/uploads
ENV OUTPUTS_DIR=/app/outputs

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY uploads/.gitkeep ./uploads/.gitkeep
COPY outputs/.gitkeep ./outputs/.gitkeep

RUN mkdir -p /app/uploads /app/outputs

EXPOSE 8000

VOLUME ["/app/uploads", "/app/outputs"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
