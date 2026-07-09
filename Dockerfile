FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY . .

# Railway sets $PORT at runtime; --workers 1 is required to keep in-memory
# scrape state consistent across all requests.
CMD gunicorn app:app --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 8 --timeout 300
