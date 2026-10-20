FROM python:3.12-slim

# system ffmpeg (faster and more complete than the bundled fallback)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PORT=8000
EXPOSE 8000
# most hosts (Render, Railway, Fly.io, Koyeb) inject $PORT
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
