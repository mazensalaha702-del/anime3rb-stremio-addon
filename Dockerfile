FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7860 \
    HOST=0.0.0.0 \
    DISPLAY=:99 \
    CHROME_PATH=/usr/bin/chromium

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        fonts-liberation \
        xvfb \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements_api.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY anime3rb_cdp_addon.py ./

EXPOSE 7860

# Start virtual display then run with python directly
CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +render -noreset & sleep 2 && python anime3rb_cdp_addon.py"]
