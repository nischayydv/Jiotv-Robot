# Use a lightweight Python image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Install essential dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies first (for caching efficiency)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files
COPY . .

# Ensure templates directory exists
RUN mkdir -p templates

# Expose ports
# Port 5000 → Flask Web App
# Port 10000 → Optional for Telegram Bot if needed
EXPOSE 5000
EXPOSE 10000

# --- HEALTH CHECKS ---
# Healthcheck for Flask app (Render-friendly)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:5000/health')" || exit 1

# --- STARTUP COMMANDS ---
# Use an environment variable to decide what to run
# BOT_MODE=true → run Telegram Bot
# Otherwise → run Flask App
CMD ["/bin/sh", "-c", "if [ \"$BOT_MODE\" = \"true\" ]; then python -u bot.py; else gunicorn --bind 0.0.0.0:5000 --workers 2 --timeout 120 app:app; fi"]
