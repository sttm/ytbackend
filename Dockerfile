FROM denoland/deno:bin-2.9.4 AS deno

FROM python:3.12-slim

COPY --from=deno /deno /usr/local/bin/deno

ENV DENO_NO_UPDATE_CHECK=1 \
    DENO_NO_PROMPT=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN deno --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN mkdir -p storage

EXPOSE 8010

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
