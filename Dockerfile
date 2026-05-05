FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7860 \
    HOST=0.0.0.0 \
    CHROME_PATH=/usr/bin/chromium

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        fonts-liberation \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements_api.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY anime3rb_pro_addon.py ./app.py

EXPOSE 7860

CMD ["python", "app.py"]
