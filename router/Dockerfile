FROM python:3.13-slim

# BtbN prunes dated autobuilds after a few weeks, so this pin rots: a rebuild
# that misses the Docker layer cache (a base-image update is enough) then 404s.
# When that happens, pick a current tag from
# https://api.github.com/repos/BtbN/FFmpeg-Builds/releases and keep the
# linux64-gpl-8.x archive -- Twitch's dual-track encapsulation needs 8.x.
ARG FFMPEG_RELEASE=autobuild-2026-09-21-13-55
ARG FFMPEG_ARCHIVE=ffmpeg-n8.1.3-linux64-gpl-8.1.tar.xz

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PATH=/opt/ffmpeg/bin:$PATH

RUN apt-get update     && apt-get install -y --no-install-recommends ca-certificates curl xz-utils     && curl --fail --location --silent --show-error         "https://github.com/BtbN/FFmpeg-Builds/releases/download/${FFMPEG_RELEASE}/${FFMPEG_ARCHIVE}"         --output /tmp/ffmpeg.tar.xz     && curl --fail --location --silent --show-error         "https://github.com/BtbN/FFmpeg-Builds/releases/download/${FFMPEG_RELEASE}/checksums.sha256"         --output /tmp/checksums.sha256     && cd /tmp     && grep " ${FFMPEG_ARCHIVE}$" checksums.sha256 | sed "s#  ${FFMPEG_ARCHIVE}#  ffmpeg.tar.xz#" | sha256sum --check --strict -     && mkdir -p /opt/ffmpeg     && tar --extract --xz --file /tmp/ffmpeg.tar.xz --directory /opt/ffmpeg --strip-components=1     && rm -rf /tmp/ffmpeg.tar.xz /tmp/checksums.sha256 /var/lib/apt/lists/*     && groupadd --gid 10001 relay     && useradd --uid 10001 --gid relay --create-home relay

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

RUN mkdir -p /data && chown -R relay:relay /data /app
USER relay

EXPOSE 8787
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--proxy-headers", "--forwarded-allow-ips", "*"]
