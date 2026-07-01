FROM python:3.11-slim-bookworm

# RTL-SDR tools, sox for audio capture, wsjtx for wsprd decoder
RUN apt-get update && apt-get install -y --no-install-recommends \
        rtl-sdr \
        sox \
        wsjtx \
        curl \
        libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent spot database goes here (mounted as a named volume)
RUN mkdir -p /data

EXPOSE 5020

CMD ["python", "app.py"]
