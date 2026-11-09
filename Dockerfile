# Worker: Claude + FFmpeg + giọng đọc + đăng TikTok. n8n gọi vào cổng 8000 (chỉ trong mạng Docker nội bộ).
FROM python:3.12-slim
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tiktok_agent ./tiktok_agent
COPY config.yaml .
RUN useradd --create-home --uid 1000 agent && mkdir -p /app/data && chown agent:agent /app/data
USER agent
EXPOSE 8000
CMD ["uvicorn", "tiktok_agent.worker:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
