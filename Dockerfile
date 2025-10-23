FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy main bot and templates
COPY bot.py .
COPY templates/ ./templates/

# Create static directory (in case it's missing)
RUN mkdir -p static

# If you have a static folder later, uncomment below
# COPY static/ ./static/

# Environment configuration
ENV PYTHONUNBUFFERED=1
ENV PORT=5000

# Expose port
EXPOSE 5000

# Run the bot
CMD ["python", "bot.py"]
