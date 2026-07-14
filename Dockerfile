FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV DISPLAY_RESOLUTION=1920x1080x24

RUN apt-get update && apt-get install -y \
    xvfb \
    chromium \
    xdotool \
    scrot \
    xclip \
    x11vnc \
    python3 \
    python3-pip \
    python3-venv \
    fonts-liberation \
    fonts-noto \
    fonts-noto-color-emoji \
    dbus-x11 \
    procps \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --break-system-packages --no-cache-dir -r /tmp/requirements.txt

COPY app/ /app/
WORKDIR /app

EXPOSE 8080 5900

CMD ["python3", "entrypoint.py"]
