FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# static ffmpeg: ~80 MB instead of ~420 MB from apt (the server disk is small)
COPY --from=mwader/static-ffmpeg:7.1.1 /ffmpeg /usr/local/bin/ffmpeg

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY assets ./assets

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown app:app /app/data
USER app

CMD ["python", "-m", "bot.main"]
