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
from urllib.parse import parse_qs, unquote, urlparse


DOWNLOAD_ROOT = Path(os.environ.get("YT_DLP_DOWNLOAD_ROOT", "/srv/downloads")).resolve()
LISTEN_HOST = os.environ.get("YT_DLP_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("YT_DLP_LISTEN_PORT", "8090"))
MAX_BODY_BYTES = 64 * 1024
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
    details = parse_job_log(job)
    return {
        "id": job["id"],
        "url": job["url"],
        "destination": savepath,
        "status": job["status"],
        "started_at": job["started_at"],
        "finished_at": job.get("finished_at"),
        "return_code": job.get("return_code"),
        **details,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "PSFFYtDlp/1.0"

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
        if self.path != "/download":
            self.send_json(404, {"error": "Not found."})
            return
        try:
            payload = self.read_payload()
            url = validate_url(payload.get("url"))
            destination = destination_from_savepath(payload.get("savepath"))
            job = start_download(url, destination)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
            return
        except OSError as error:
            self.send_json(500, {"error": f"Could not start yt-dlp: {error}"})
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
