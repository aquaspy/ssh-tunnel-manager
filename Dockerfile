FROM python:3.9-slim

# Install required packages
RUN apt-get update && apt-get install -y \
    openssh-client \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Create a non-root user
RUN groupadd -g 1000 appuser && \
    useradd -u 1000 -g 1000 -ms /bin/bash appuser

# Copy requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .
COPY templates templates/

# Create directory for SSH keys and config
RUN mkdir -p /data && \
    chown -R appuser:appuser /data && \
    chmod 755 /data

# Create entrypoint script BEFORE switching to non-root user
RUN echo '#!/bin/bash\n\
mkdir -p /data/.ssh\n\
chmod 700 /data/.ssh\n\
exec python /app/app.py\n' > /app/entrypoint.sh \
    && chmod +x /app/entrypoint.sh

# Make sure application directory is writable by non-root user
RUN chown -R appuser:appuser /app

# Set environment variables
ENV HOME=/data
ENV PYTHONUNBUFFERED=1

# Switch to non-root user
USER appuser

EXPOSE 5000

ENTRYPOINT ["/app/entrypoint.sh"]
