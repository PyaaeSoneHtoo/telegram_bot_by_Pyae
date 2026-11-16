FROM python:3.11-slim

# Install system dependencies: ffmpeg, nodejs, curl, unzip, ca-certificates, and deno
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    curl \
    unzip \
    ca-certificates \
    && curl -fsSL https://deno.land/install.sh | sh \
    && cp /root/.deno/bin/deno /usr/local/bin/deno \
    && chmod +x /usr/local/bin/deno \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python packages with latest yt-dlp and curl-cffi
COPY requirements.txt .
RUN pip install --no-cache-dir --pre -U "yt-dlp[default,curl-cffi]" && \
    pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Run bot
CMD ["python", "bot.py"]
