FROM python:3.11-slim-bookworm
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends libimage-exiftool-perl tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock ./requirements.lock
RUN PIP_CERT=/etc/ssl/certs/ca-certificates.crt pip install --no-cache-dir -r requirements.lock
COPY interval_photos.py web_app.py image_preview.py copy_jobs.py ./
COPY templates ./templates
COPY static ./static
RUN chmod -R a+rX /app
ENV PHOTO_ROOT=/photos PHOTO_TIMEZONE=Asia/Tokyo PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ARG APP_UID=65532
ARG APP_GID=65532
USER ${APP_UID}:${APP_GID}
EXPOSE 8080
CMD ["python", "web_app.py"]
