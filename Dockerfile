FROM python:3.11-slim-bookworm

# wsjtx provides wsprd; sox is used for any future audio work
RUN apt-get update && apt-get install -y --no-install-recommends \
        wsjtx sox curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

CMD ["python", "app.py"]
