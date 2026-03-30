FROM python:3.11-slim

# Системные зависимости для Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libcups2 \
    libxss1 \
    libgtk-3-0 \
    libxshmfence1 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python-зависимости
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright-Browser
RUN playwright install chromium
RUN playwright install-deps chromium

# App-Code
COPY backend/ ./
COPY backend/static/ ./static/

# Cloud-Modus: headless
ENV HEADLESS=true
ENV PORT=8000

EXPOSE 8000

CMD ["python", "server.py"]
