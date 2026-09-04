FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.30" "httpx>=0.27" "pyyaml>=6"
WORKDIR /app
COPY app /app/app
ENV PYTHONUNBUFFERED=1
EXPOSE 8790
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8790", "--proxy-headers", "--forwarded-allow-ips", "*"]
