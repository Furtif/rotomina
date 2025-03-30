FROM ubuntu:22.04

# Set non-interactive installation
ENV DEBIAN_FRONTEND=noninteractive

# Install dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-dev \
    android-tools-adb \
    wget \
    unzip \
    curl \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Create necessary directories
RUN mkdir -p /app/data/apks/pogo/extracted
RUN mkdir -p /app/data/modules/playintegrityfix
RUN mkdir -p /app/static/css
RUN chmod -R 755 /app

# Setup adb server to start automatically
RUN echo "#!/bin/bash\nadb start-server\nexec uvicorn main:app --host 0.0.0.0 --port 8000" > /app/start.sh
RUN chmod +x /app/start.sh

# Expose port
EXPOSE 8000

# Run server
CMD ["/app/start.sh"]
