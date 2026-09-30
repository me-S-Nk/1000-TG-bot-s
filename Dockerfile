# Production Dockerfile for Telegram Bot «1000 мелочей»
FROM python:3.12-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# Working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency specifications
COPY requirements.txt .

# Install Python packages
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Copy project files
COPY . .

# Set execution permissions and create non-root runtime user
RUN chmod +x /app/entrypoint.sh && \
    useradd -m -u 1000 botuser && \
    chown -R botuser:botuser /app

USER botuser

# Startup entrypoint running migrations followed by the bot process
ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
