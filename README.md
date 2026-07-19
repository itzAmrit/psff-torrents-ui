# psff-torrents-ui

Standalone torrent control UI extracted from `tsff-ui`.

This repo contains the static fallback torrent frontend. It is meant to sit in front of qBittorrent plus the TSFF sync agent.

This repo is the source of truth for the custom torrent UI served at `torrents.paidshitforfree.com/fallback/`.

## What this app needs

- qBittorrent Web API under `/api/v2/...`
- TSFF GCS sync agent under `/api/gcs-sync/...`
- Writable downloads surface under `/__downloads__/...`

The UI uses all of those directly from the browser, so the reverse proxy on the same host must expose them.

## yt-dlp downloads

The UI image includes a small local worker around the `yt-dlp` CLI. It accepts
downloads at `POST /yt-dlp/download`, writes them beneath the mounted
`/srv/downloads` directory, and exposes live jobs at `GET /yt-dlp/jobs/`.
Individual jobs support `GET` and `DELETE` at `/yt-dlp/jobs/:id`.

The browser polls the jobs endpoint every two seconds and shows percentage,
speed, ETA, destination, completion or failure state, and a cancel action.

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

## Docker Artifact

The production UI is packaged as its own Docker image:

```bash
us-central1-docker.pkg.dev/psff-cloud-run-496515/psff-images/psff-torrents-ui:<tag>
```

The container serves static assets on port `80`. On the VM, nginx stays responsible for exposing `/`, `/api/`, `/api/gcs-sync/`, and `/__downloads__/`.

## Deployment

Primary flow:

1. push to `main`
2. Cloud Build builds `us-central1-docker.pkg.dev/$PROJECT_ID/psff-images/psff-torrents-ui`
3. Cloud Build pushes:
   - `latest`
   - `sha-<shortSha>`
4. Cloud Build publishes a deploy request to the `psff-gitops-deploys` Pub/Sub topic
5. the `psff-gitops` Pub/Sub deploy trigger updates `versions.torrents-ui.env`
6. `psff-gitops` deploys the new tag onto `tsff-ui-vm`

Cloud Build config:

- [cloudbuild.yaml](/Users/null/Downloads/Codes/live/psff-torrents-ui/cloudbuild.yaml)

GitHub Actions is manual fallback only via:

- [.github/workflows/cloud-build-handoff.yml](/Users/null/Downloads/Codes/live/psff-torrents-ui/.github/workflows/cloud-build-handoff.yml)
