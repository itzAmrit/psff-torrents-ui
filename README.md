# psff-torrents-ui

Standalone torrent control UI extracted from `tsff-ui`.

This repo contains the static fallback torrent frontend. It is meant to sit in front of qBittorrent plus the TSFF sync agent.

## What this app needs

- qBittorrent Web API under `/api/v2/...`
- TSFF GCS sync agent under `/api/gcs-sync/...`
- Writable downloads surface under `/__downloads__/...`

The UI uses all of those directly from the browser, so the reverse proxy on the same host must expose them.

## Run the UI only

```bash
docker build -t psff-torrents-ui .
docker run --rm -p 8080:80 psff-torrents-ui
```

That serves the frontend, but most actions will fail until the backend routes exist.

## Backend wiring

Use [`backend-example.nginx.conf`](./backend-example.nginx.conf) as the starting point for a host that should expose:

- the torrent UI at `/`
- qBittorrent at `/api/`
- the GCS sync agent at `/api/gcs-sync/`
- downloads/file operations at `/__downloads__/`

Replace:

- `http://qbittorrent:8080` with your qBittorrent upstream
- `http://127.0.0.1:9099` with your sync-agent upstream if different
- `/srv/downloads/` with your actual downloads path

## Quick docker-compose

```bash
docker compose -f docker-compose.example.yml up --build -d
```
