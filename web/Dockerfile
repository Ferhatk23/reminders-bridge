# Eigenständige Auslieferung der Oberfläche. Nginx serviert die statischen
# Dateien und leitet /v1 an die API weiter — dabei setzt es den API-Token als
# Header, sodass er nie im Browser landet.
FROM nginx:alpine
COPY web/public /usr/share/nginx/html/static
COPY web/public/index.html /usr/share/nginx/html/index.html
COPY web/public/favicon.svg web/public/manifest.webmanifest /usr/share/nginx/html/
COPY web/nginx.conf.template /etc/nginx/templates/default.conf.template
ENV API_URL=http://api:8000 API_TOKEN=""
