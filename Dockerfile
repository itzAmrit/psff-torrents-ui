FROM nginx:alpine

RUN apk add --no-cache ffmpeg python3 py3-pip yt-dlp \
    && python3 -m pip install --break-system-packages --no-cache-dir --target /opt/transferit transferit-py==0.1.0rc1

ENV PYTHONPATH=/opt/transferit

COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.d/10-start-yt-dlp.sh /docker-entrypoint.d/10-start-yt-dlp.sh
COPY services/yt_dlp_server.py /opt/psff/yt_dlp_server.py
COPY index.html /usr/share/nginx/html/index.html
COPY restore-bootstrap.js /usr/share/nginx/html/restore-bootstrap.js

RUN chmod 755 /docker-entrypoint.d/10-start-yt-dlp.sh
