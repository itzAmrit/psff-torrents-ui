#!/usr/bin/env python3

import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen


DOWNLOAD_ROOT = Path(os.environ.get("YT_DLP_DOWNLOAD_ROOT", "/srv/downloads")).resolve()
LISTEN_HOST = os.environ.get("YT_DLP_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("YT_DLP_LISTEN_PORT", "8090"))
MAX_BODY_BYTES = 64 * 1024
SYNC_AGENT_URL = os.environ.get("TSFF_SYNC_AGENT_URL", "http://torrents-sync-agent:9099").rstrip("/")
SYNC_WAIT_SECONDS = int(os.environ.get("TRANSFER_IT_SYNC_WAIT_SECONDS", "7200"))
SYNC_BACKGROUND_WAIT_SECONDS = max(
    SYNC_WAIT_SECONDS,
    int(os.environ.get("TRANSFER_IT_SYNC_BACKGROUND_WAIT_SECONDS", "86400")),
)
SYNC_POLL_SECONDS = max(2, int(os.environ.get("TRANSFER_IT_SYNC_POLL_SECONDS", "2")))
jobs = {}
processes = {}
jobs_lock = threading.Lock()

PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
SPEED_RE = re.compile(r"\sat\s+(\S+)")
ETA_RE = re.compile(r"\sETA\s+(\S+)")
DESTINATION_RE = re.compile(
    r"(?:Destination:|Merging formats into)\s+[\"']?(.+?)[\"']?$"
)
POSTPROCESS_TARGET_RE = re.compile(
    r"\[(?:Fixup[^]]*|VideoRemuxer|VideoConvertor|ExtractAudio|Metadata|EmbedSubtitle|EmbedThumbnail)\].*?[\"'](.+?)[\"']"
)
FFMPEG_DURATION_RE = re.compile(r"Duration:\s+(\d+):(\d+):(\d+(?:\.\d+)?)")
FFMPEG_TIME_RE = re.compile(r"\btime=(\d+):(\d+):(\d+(?:\.\d+)?)")
FFMPEG_SPEED_RE = re.compile(r"\bspeed=\s*(\d+(?:\.\d+)?)x")
POSTPROCESS_MARKERS = (
    "[Merger]",
    "[Fixup",
    "[VideoRemuxer]",
    "[VideoConvertor]",
    "[ExtractAudio]",
    "[Metadata]",
    "[EmbedSubtitle]",
    "[EmbedThumbnail]",
)


def destination_from_savepath(savepath):
    value = unquote(str(savepath or "/downloads")).strip()
    if value == "/downloads":
        relative = ""
    elif value.startswith("/downloads/"):
        relative = value[len("/downloads/") :]
    else:
        raise ValueError("Destination must be inside /downloads.")

    destination = (DOWNLOAD_ROOT / relative).resolve()
    try:
        destination.relative_to(DOWNLOAD_ROOT)
    except ValueError as error:
        raise ValueError("Destination must be inside /downloads.") from error
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def validate_url(value):
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("yt-dlp needs a valid HTTP or HTTPS URL.")
    return url


def validate_transfer_it_url(value):
    url = validate_url(value)
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in {"transfer.it", "www.transfer.it"}:
        raise ValueError("Transfer.it downloads need a transfer.it URL.")
    path_parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(path_parts) != 2 or path_parts[0].lower() != "t" or not path_parts[1]:
        raise ValueError("Transfer.it URL must look like https://transfer.it/t/<handle>.")
    return url


def finish_job(job_id, process, log_handle):
    return_code = process.wait()
    log_handle.close()
    with jobs_lock:
        processes.pop(job_id, None)
        job = jobs.get(job_id)
        if job is not None:
            if job["status"] != "cancelled":
                job["status"] = "completed" if return_code == 0 else "failed"
            job["return_code"] = return_code
            job["finished_at"] = int(time.time())


def start_download(url, destination):
    job_id = uuid.uuid4().hex
    log_path = destination / f".yt-dlp-{job_id}.log"
    log_handle = log_path.open("ab")
    command = [
        "yt-dlp",
        "--newline",
        "--no-colors",
        "--restrict-filenames",
        "--paths",
        str(destination),
        "--output",
        "%(title).200B [%(id)s].%(ext)s",
        url,
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    job = {
        "id": job_id,
        "url": url,
        "destination": str(destination),
        "status": "running",
        "pid": process.pid,
        "log_path": str(log_path),
        "started_at": int(time.time()),
    }
    with jobs_lock:
        jobs[job_id] = job
        processes[job_id] = process
    threading.Thread(
        target=finish_job,
        args=(job_id, process, log_handle),
        daemon=True,
    ).start()
    return job


class TransferItCancelled(Exception):
    pass


def sync_agent_request(path, method="GET"):
    request = Request(
        f"{SYNC_AGENT_URL}{path}",
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        data=b"{}" if method != "GET" else None,
    )
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_sync_and_cleanup(
    job_id,
    completed_paths,
    destination,
    cancel_event,
    *,
    wait_seconds=None,
    allow_background=True,
):
    """Require a successful bucket sync before deleting transfer.it files."""
    _update_transfer_job(job_id, phase="syncing", sync_status="starting")
    try:
        before = sync_agent_request("/api/gcs-sync/status")
        before_runtime = before.get("runtime") or {}
        baseline_finished = str(before_runtime.get("last_finished_at") or "")
        baseline_started = str(before_runtime.get("last_started_at") or "")
        started = sync_agent_request("/api/gcs-sync/run", method="POST")
    except (HTTPError, URLError, OSError, ValueError) as error:
        raise RuntimeError(f"Could not start bucket sync; files were kept on the VM: {error}") from error

    started_at = time.time()
    deadline = started_at + max(60, wait_seconds or SYNC_WAIT_SECONDS)
    last_run_started = ""
    while time.time() < deadline:
        if cancel_event.is_set():
            raise TransferItCancelled("Download cancelled while waiting for bucket sync.")
        try:
            status = sync_agent_request("/api/gcs-sync/status")
        except (HTTPError, URLError, OSError, ValueError) as error:
            raise RuntimeError(f"Could not check bucket sync; files were kept on the VM: {error}") from error
        runtime = status.get("runtime") or {}
        config = status.get("config") or {}
        if not config.get("enabled") or not str(config.get("bucket_name") or "").strip():
            raise RuntimeError("Bucket sync is not enabled; files were kept on the VM.")
        last_run_started = str(runtime.get("last_started_at") or last_run_started)
        if runtime.get("running"):
            _update_transfer_job(job_id, sync_status="syncing")
            time.sleep(SYNC_POLL_SECONDS)
            continue
        if runtime.get("last_result") == "error":
            detail = str(runtime.get("last_error") or "unknown bucket sync error")
            raise RuntimeError(f"Bucket sync failed; files were kept on the VM: {detail}")
        last_finished = str(runtime.get("last_finished_at") or "")
        run_changed = (
            (last_finished and last_finished != baseline_finished)
            or (last_run_started and last_run_started != baseline_started)
        )
        sync_stats = runtime.get("last_stats") or {}
        download_stats = sync_stats.get("downloads") or {}
        sync_verified = (
            int(download_stats.get("missing_remote") or 0) == 0
            and int(download_stats.get("mismatched") or 0) == 0
        )
        if runtime.get("last_result") == "ok" and run_changed and sync_verified:
            if started.get("started"):
                break
            # The first request was queued behind an older run. Start a new
            # run now that the older one has finished, so it definitely sees
            # the files this transfer just created.
            baseline_finished = last_finished
            baseline_started = last_run_started
            started = sync_agent_request("/api/gcs-sync/run", method="POST")
            _update_transfer_job(job_id, sync_status="syncing")
            time.sleep(SYNC_POLL_SECONDS)
            continue
        if runtime.get("last_result") == "ok" and run_changed and not sync_verified:
            raise RuntimeError("Bucket sync completed without verifying every download object; files were kept on the VM.")
        # If another sync was already running when we requested one, make a
        # second request after it finishes so this transfer is definitely in
        # the completed run rather than merely an older successful run.
        if not started.get("started"):
            started = sync_agent_request("/api/gcs-sync/run", method="POST")
        _update_transfer_job(job_id, sync_status="waiting")
        time.sleep(SYNC_POLL_SECONDS)
    else:
        if allow_background:
            # A large transfer can outlive the foreground wait even though the
            # sync agent is healthy and will finish on its next scheduled run.
            # Keep the job in syncing state and continue monitoring rather than
            # recording a permanent false failure.
            _update_transfer_job(
                job_id,
                phase="syncing",
                sync_status="waiting",
                error=None,
                sync_waiting_since=int(time.time()),
            )
            threading.Thread(
                target=continue_sync_wait,
                args=(job_id, completed_paths, destination, cancel_event),
                daemon=True,
            ).start()
            return False
        raise RuntimeError("Bucket sync did not finish before the timeout; files were kept on the VM.")

    removed = 0
    for raw_path in completed_paths:
        path = Path(raw_path).resolve()
        try:
            path.relative_to(destination.resolve())
        except ValueError as error:
            raise RuntimeError("Refusing to remove a transfer path outside its destination.") from error
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed += 1
    for raw_path in sorted(completed_paths, key=lambda value: len(str(value)), reverse=True):
        current = Path(raw_path).resolve().parent
        root = destination.resolve()
        while current != root:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent
    _update_transfer_job(job_id, sync_status="synced_and_removed", removed_files=removed)
    return True


def mark_transfer_job_completed(job_id):
    with jobs_lock:
        current = jobs.get(job_id)
        if current is not None:
            current["status"] = "completed"
            current["phase"] = "completed"
            current["bytes_done"] = current.get("bytes_total", current.get("bytes_done", 0))
            current["finished_at"] = int(time.time())


def continue_sync_wait(job_id, completed_paths, destination, cancel_event):
    try:
        if wait_for_sync_and_cleanup(
            job_id,
            completed_paths,
            destination,
            cancel_event,
            wait_seconds=SYNC_BACKGROUND_WAIT_SECONDS,
            allow_background=False,
        ):
            mark_transfer_job_completed(job_id)
    except TransferItCancelled as error:
        _update_transfer_job(
            job_id,
            status="cancelled",
            phase="cancelled",
            error=str(error),
            finished_at=int(time.time()),
        )
    except Exception as error:
        _update_transfer_job(
            job_id,
            status="failed",
            phase="failed",
            error=str(error),
            finished_at=int(time.time()),
        )


def _update_transfer_job(job_id, **updates):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is not None:
            job.update(updates)


def start_transfer_it_download(url, destination, password):
    job_id = uuid.uuid4().hex
    cancel_event = threading.Event()
    job = {
        "id": job_id,
        "kind": "transfer-it",
        "url": url,
        "destination": str(destination),
        "status": "running",
        "started_at": int(time.time()),
        "finished_at": None,
        "return_code": None,
        "cancel_event": cancel_event,
        "bytes_done": 0,
        "bytes_total": 0,
        "completed_bytes": 0,
        "current_file": None,
        "current_file_done": 0,
        "current_file_total": 0,
        "download_speed": None,
        "download_eta": None,
        "error": None,
        "phase": "downloading",
        "sync_status": None,
        "removed_files": 0,
        "file_count": 0,
    }
    with jobs_lock:
        jobs[job_id] = job

    def run():
        try:
            from transferit import Transferit
            completed_paths = []

            def check_cancelled():
                if cancel_event.is_set():
                    raise TransferItCancelled("Download cancelled.")

            def on_start(files, total):
                check_cancelled()
                _update_transfer_job(
                    job_id,
                    bytes_total=int(total or 0),
                    file_count=len(files),
                )

            def on_file_start(node, out_path):
                check_cancelled()
                _update_transfer_job(
                    job_id,
                    current_file=getattr(node, "name", None) or out_path.name,
                    current_file_done=0,
                    current_file_total=int(getattr(node, "size", 0) or 0),
                )

            def on_file_progress(node, done, total):
                check_cancelled()
                with jobs_lock:
                    current = jobs.get(job_id)
                    if current is None:
                        return
                    completed = int(current.get("completed_bytes", 0))
                    started = int(current.get("started_at", int(time.time())))
                    bytes_done = completed + int(done or 0)
                    elapsed = max(0.001, time.time() - started)
                    speed = bytes_done / elapsed
                    remaining = max(0, int(total or 0) - int(done or 0))
                    current.update(
                        {
                            "bytes_done": bytes_done,
                            "current_file_done": int(done or 0),
                            "current_file_total": int(total or 0),
                            "download_speed": f"{speed / (1024 * 1024):.2f} MiB/s",
                            "download_eta": format_eta(remaining / speed) if speed > 0 else None,
                        }
                    )

            def on_file_done(node, out_path):
                check_cancelled()
                with jobs_lock:
                    current = jobs.get(job_id)
                    if current is None:
                        return
                    completed = int(current.get("completed_bytes", 0)) + int(
                        getattr(node, "size", 0) or current.get("current_file_total", 0) or 0
                    )
                    current.update(
                        {
                            "completed_bytes": completed,
                            "bytes_done": completed,
                            "current_file_done": int(current.get("current_file_total", 0)),
                        }
                    )
                completed_paths.append(str(out_path))

            with Transferit() as client:
                client.download(
                    url,
                    destination,
                    password=password or None,
                    on_start=on_start,
                    on_file_start=on_file_start,
                    on_file_progress=on_file_progress,
                    on_file_done=on_file_done,
                )
            if not wait_for_sync_and_cleanup(job_id, completed_paths, destination, cancel_event):
                return
            mark_transfer_job_completed(job_id)
        except TransferItCancelled as error:
            _update_transfer_job(
                job_id,
                status="cancelled",
                phase="cancelled",
                error=str(error),
                finished_at=int(time.time()),
            )
        except Exception as error:  # transfer.it surfaces protocol/network errors here
            _update_transfer_job(
                job_id,
                status="failed",
                phase="failed",
                error=str(error),
                finished_at=int(time.time()),
            )

    threading.Thread(target=run, daemon=True).start()
    return job


def clock_seconds(hours, minutes, seconds):
    return (int(hours) * 3600) + (int(minutes) * 60) + float(seconds)


def format_eta(total_seconds):
    value = max(0, int(total_seconds))
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def parse_job_log(job):
    result = {
        "phase": "completed" if job.get("status") == "completed" else "downloading",
        "download_progress": 100.0 if job.get("status") == "completed" else 0.0,
        "download_speed": None,
        "download_eta": None,
        "postprocess_progress": 100.0 if job.get("status") == "completed" else None,
        "postprocess_speed": None,
        "postprocess_eta": None,
        "has_postprocess": False,
        "filename": None,
        "error": None,
    }
    log_path = Path(job["log_path"])
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 128 * 1024))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return result

    ffmpeg_duration = None
    ffmpeg_time = None
    ffmpeg_speed = None
    saw_postprocess = False
    for line in text.replace("\r", "\n").splitlines():
        progress_match = PROGRESS_RE.search(line)
        if progress_match:
            result["download_progress"] = float(progress_match.group(1))
            speed_match = SPEED_RE.search(line)
            eta_match = ETA_RE.search(line)
            result["download_speed"] = speed_match.group(1) if speed_match else result["download_speed"]
            result["download_eta"] = eta_match.group(1) if eta_match else result["download_eta"]
        destination_match = DESTINATION_RE.search(line)
        if destination_match:
            result["filename"] = Path(destination_match.group(1)).name
        postprocess_target_match = POSTPROCESS_TARGET_RE.search(line)
        if postprocess_target_match:
            result["filename"] = Path(postprocess_target_match.group(1)).name
        if any(marker in line for marker in POSTPROCESS_MARKERS):
            saw_postprocess = True
        duration_match = FFMPEG_DURATION_RE.search(line)
        if duration_match:
            ffmpeg_duration = clock_seconds(*duration_match.groups())
        time_match = FFMPEG_TIME_RE.search(line)
        if time_match:
            ffmpeg_time = clock_seconds(*time_match.groups())
            saw_postprocess = True
        ffmpeg_speed_match = FFMPEG_SPEED_RE.search(line)
        if ffmpeg_speed_match:
            ffmpeg_speed = float(ffmpeg_speed_match.group(1))
        if line.startswith("ERROR:"):
            result["error"] = line.removeprefix("ERROR:").strip()

    if saw_postprocess and job.get("status") == "running":
        result["phase"] = "postprocessing"
        result["download_progress"] = 100.0
        result["download_eta"] = None
        if ffmpeg_duration and ffmpeg_time is not None:
            result["postprocess_progress"] = max(
                0.0,
                min(99.9, (ffmpeg_time / ffmpeg_duration) * 100),
            )
            if ffmpeg_speed and ffmpeg_speed > 0:
                result["postprocess_speed"] = f"{ffmpeg_speed:.2f}x"
                result["postprocess_eta"] = format_eta(
                    (ffmpeg_duration - ffmpeg_time) / ffmpeg_speed
                )
        if result["postprocess_progress"] is None and result["filename"]:
            final_path = Path(job["destination"]) / result["filename"]
            temp_path = final_path.with_name(f"{final_path.stem}.temp{final_path.suffix}")
            try:
                source_size = final_path.stat().st_size
                temp_size = temp_path.stat().st_size
                if source_size > 0:
                    result["postprocess_progress"] = max(
                        0.0,
                        min(99.9, (temp_size / source_size) * 100),
                    )
            except OSError:
                pass
    elif job.get("status") == "failed":
        result["phase"] = "failed"
    elif job.get("status") == "cancelled":
        result["phase"] = "cancelled"

    result["has_postprocess"] = saw_postprocess
    result["progress"] = (
        result["postprocess_progress"]
        if result["phase"] == "postprocessing" and result["postprocess_progress"] is not None
        else result["download_progress"]
    )
    result["speed"] = (
        result["postprocess_speed"]
        if result["phase"] == "postprocessing"
        else result["download_speed"]
    )
    result["eta"] = (
        result["postprocess_eta"]
        if result["phase"] == "postprocessing"
        else result["download_eta"]
    )
    return result


def public_job(job):
    destination = Path(job["destination"])
    try:
        relative = destination.relative_to(DOWNLOAD_ROOT)
        savepath = "/downloads" if not relative.parts else f"/downloads/{relative.as_posix()}"
    except ValueError:
        savepath = "/downloads"
    if job.get("kind") == "transfer-it":
        started = int(job.get("started_at") or 0)
        finished = int(job.get("finished_at") or time.time())
        bytes_total = int(job.get("bytes_total") or 0)
        bytes_done = int(job.get("bytes_done") or 0)
        elapsed = max(0.001, finished - started) if job.get("status") != "running" else max(0.001, time.time() - started)
        speed_bytes = bytes_done / elapsed
        remaining = max(0, bytes_total - bytes_done)
        details = {
            "phase": job.get("phase") or ("downloading" if job.get("status") == "running" else job.get("status")),
            "download_progress": (bytes_done / bytes_total * 100) if bytes_total else 0.0,
            "download_speed": job.get("download_speed") or (f"{speed_bytes / (1024 * 1024):.2f} MiB/s" if speed_bytes else None),
            "download_eta": job.get("download_eta") or (format_eta(remaining / speed_bytes) if speed_bytes and remaining else None),
            "postprocess_progress": None,
            "postprocess_speed": None,
            "postprocess_eta": None,
            "has_postprocess": False,
            "filename": job.get("current_file"),
            "error": job.get("error"),
            "bytes_done": bytes_done,
            "bytes_total": bytes_total,
            "file_count": int(job.get("file_count") or 0),
            "sync_status": job.get("sync_status"),
            "removed_files": int(job.get("removed_files") or 0),
        }
        details["progress"] = details["download_progress"]
        details["speed"] = details["download_speed"]
        details["eta"] = details["download_eta"]
    else:
        details = parse_job_log(job)
    return {
        "id": job["id"],
        "kind": job.get("kind", "yt-dlp"),
        "url": job["url"],
        "destination": savepath,
        "status": job["status"],
        "started_at": job["started_at"],
        "finished_at": job.get("finished_at"),
        "return_code": job.get("return_code"),
        **details,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "PSFFDownloadWorker/1.1"

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_payload(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid Content-Length.") from error
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("Request body is empty or too large.")
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("application/json"):
            payload = json.loads(body.decode("utf-8"))
            return {key: str(value) for key, value in payload.items()}
        values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        return {key: entries[-1] for key, entries in values.items() if entries}

    def do_POST(self):
        if self.path not in {"/download", "/transfer-it/download"}:
            self.send_json(404, {"error": "Not found."})
            return
        try:
            payload = self.read_payload()
            is_transfer_it = self.path == "/transfer-it/download" or payload.get("kind") == "transfer-it"
            url = validate_transfer_it_url(payload.get("url")) if is_transfer_it else validate_url(payload.get("url"))
            destination = destination_from_savepath(payload.get("savepath"))
            job = start_transfer_it_download(url, destination, payload.get("password")) if is_transfer_it else start_download(url, destination)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
            return
        except OSError as error:
            self.send_json(500, {"error": f"Could not start download: {error}"})
            return
        self.send_json(
            202,
            public_job(job),
        )

    def do_GET(self):
        clean_path = self.path.split("?", 1)[0]
        if clean_path in {"/jobs", "/jobs/"}:
            with jobs_lock:
                items = [dict(job) for job in jobs.values()]
            items.sort(key=lambda job: job["started_at"], reverse=True)
            self.send_json(200, {"jobs": [public_job(job) for job in items]})
            return
        if not clean_path.startswith("/jobs/"):
            self.send_json(404, {"error": "Not found."})
            return
        job_id = clean_path.removeprefix("/jobs/")
        with jobs_lock:
            job = dict(jobs.get(job_id) or {})
        if not job:
            self.send_json(404, {"error": "Download job not found."})
            return
        self.send_json(200, public_job(job))

    def do_DELETE(self):
        clean_path = self.path.split("?", 1)[0]
        if not clean_path.startswith("/jobs/"):
            self.send_json(404, {"error": "Not found."})
            return
        job_id = clean_path.removeprefix("/jobs/")
        with jobs_lock:
            job = jobs.get(job_id)
            process = processes.get(job_id)
            if job is None:
                self.send_json(404, {"error": "Download job not found."})
                return
            if process is None or process.poll() is not None:
                if job.get("kind") == "transfer-it" and job.get("status") == "running":
                    cancel_event = job.get("cancel_event")
                    if cancel_event is not None:
                        cancel_event.set()
                        job["status"] = "cancelled"
                        job["finished_at"] = int(time.time())
                        job["error"] = "Download cancelled."
                        self.send_json(200, public_job(dict(job)))
                        return
                self.send_json(409, {"error": "Download job is no longer running."})
                return
            job["status"] = "cancelled"
            job["finished_at"] = int(time.time())
            os.killpg(process.pid, signal.SIGTERM)
            response = public_job(dict(job))
        self.send_json(200, response)

    def log_message(self, message, *args):
        print(
            f'{self.address_string()} - [{self.log_date_time_string()}] {message % args}',
            flush=True,
        )


if __name__ == "__main__":
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()
