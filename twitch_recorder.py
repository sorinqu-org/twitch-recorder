#!/usr/bin/env python3
"""Continuously record Twitch channels and hand completed chunks to a processor."""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import yaml

LOG = logging.getLogger("twitch-recorder")
CHUNK_RE = re.compile(r"^chunk_(\d+)\.mp4$")

# Pause recording when free space drops below this instead of letting ffmpeg die
# on a full disk (which used to trigger an endless reconnect loop that spawned a
# new empty chunk every poll). 4 GiB leaves room for the active chunk to finalize
# and for the processor's scratch space.
MIN_FREE_BYTES = 4 * 1024**3
# Quarantine a chunk after this many consecutive "invalid MP4" checks so a broken
# recording can't be re-enqueued by the folder scan forever.
INVALID_QUARANTINE_THRESHOLD = 3


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    oauth_token: str
    channels: tuple[str, ...]
    download_directory: Path
    check_interval: int
    chunk_duration: int
    quality: str
    max_quality: str
    rclone_remote: str
    rclone_base_path: str
    rclone_config: str
    delete_after_upload: bool
    upload_workers: int
    processor_command: tuple[str, ...]
    processor_max_attempts: int
    processor_retry_initial_seconds: int
    processor_retry_max_seconds: int
    processor_timeout_seconds: int

    @classmethod
    def load(cls, path: Path) -> Settings:
        raw = expand_env(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        twitch = raw.get("twitch", {})
        credentials_file = str(twitch.get("credentials_file", "")).strip()
        if credentials_file:
            credentials_path = Path(credentials_file).expanduser()
            credentials = expand_env(yaml.safe_load(credentials_path.read_text(encoding="utf-8")) or {})
            stored_twitch = credentials.get("twitch", credentials)
            twitch = {**stored_twitch, **{key: value for key, value in twitch.items() if key != "credentials_file"}}
        upload = raw.get("rclone", {})
        processor = raw.get("processor", {})
        processor_command_raw = processor.get("command", [])
        if processor_command_raw and not isinstance(processor_command_raw, list):
            raise ValueError("processor.command must be a YAML list")
        channels = tuple(dict.fromkeys(str(c).strip().lower() for c in raw.get("channels", []) if str(c).strip()))
        settings = cls(
            client_id=str(twitch.get("client_id", "")).strip(),
            client_secret=str(twitch.get("client_secret", "")).strip(),
            oauth_token=str(twitch.get("oauth_token", "")).removeprefix("oauth:").strip(),
            channels=channels,
            download_directory=Path(str(raw.get("download_directory", "./downloads"))).expanduser().resolve(),
            check_interval=max(15, int(raw.get("check_interval", 60))),
            chunk_duration=max(60, int(raw.get("chunk_duration", 3600))),
            quality=str(raw.get("quality", "best")),
            max_quality=str(raw.get("max_quality", "1080p")).strip(),
            rclone_remote=str(upload.get("remote", "")).rstrip(":"),
            rclone_base_path=str(upload.get("base_path", "Twitch")).strip("/"),
            rclone_config=str(upload.get("config", "")).strip(),
            delete_after_upload=bool(upload.get("delete_after_upload", False)),
            upload_workers=max(1, int(upload.get("workers", 2))),
            processor_command=tuple(str(item) for item in processor_command_raw),
            processor_max_attempts=max(1, int(processor.get("max_attempts", 8))),
            processor_retry_initial_seconds=max(
                1, int(processor.get("retry_initial_seconds", 60))
            ),
            processor_retry_max_seconds=max(
                1, int(processor.get("retry_max_seconds", 1800))
            ),
            processor_timeout_seconds=max(
                60, int(processor.get("timeout_seconds", 18000))
            ),
        )
        if not settings.client_id or not settings.client_secret:
            raise ValueError("twitch.client_id and twitch.client_secret are required")
        if "$" in settings.client_id or "$" in settings.client_secret:
            raise ValueError("Twitch environment variables are not set")
        if not settings.channels:
            raise ValueError("at least one channel is required")
        if not settings.rclone_remote and not settings.processor_command:
            raise ValueError("rclone.remote or processor.command is required")
        return settings


class TwitchHelix:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self._token = ""
        self._token_deadline = 0.0

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_deadline:
            return self._token
        response = self.session.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "grant_type": "client_credentials",
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        self._token = data["access_token"]
        self._token_deadline = time.monotonic() + max(60, int(data.get("expires_in", 3600)) - 120)
        return self._token

    def live_streams(self) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        headers = {
            "Client-ID": self.settings.client_id,
            "Authorization": f"Bearer {self._access_token()}",
        }
        for offset in range(0, len(self.settings.channels), 100):
            channels = self.settings.channels[offset : offset + 100]
            response = self.session.get(
                "https://api.twitch.tv/helix/streams",
                headers=headers,
                params=[("user_login", channel) for channel in channels],
                timeout=20,
            )
            if response.status_code == 401:
                self._token = ""
                headers["Authorization"] = f"Bearer {self._access_token()}"
                response = self.session.get(
                    "https://api.twitch.tv/helix/streams",
                    headers=headers,
                    params=[("user_login", channel) for channel in channels],
                    timeout=20,
                )
            response.raise_for_status()
            for stream in response.json().get("data", []):
                result[stream["user_login"].lower()] = {
                    "id": stream["id"],
                    "started_at": stream["started_at"],
                }
        return result


def stream_folder(root: Path, channel: str, started_at: str) -> Path:
    started = datetime.fromisoformat(started_at).astimezone()
    return root / channel / started.strftime("%Y-%m-%d_%H-%M-%S")


def chunk_number(path: Path) -> int | None:
    match = CHUNK_RE.match(path.name)
    return int(match.group(1)) if match else None


def next_chunk_number(folder: Path) -> int:
    numbers = [number for path in folder.glob("chunk_*.mp4") if (number := chunk_number(path)) is not None]
    return max(numbers, default=0) + 1


def valid_mp4(path: Path, ffprobe: str = "ffprobe") -> bool:
    if not path.is_file() or path.stat().st_size < 10_000:
        return False
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0:
        return False
    try:
        return float(json.loads(probe.stdout)["format"]["duration"]) > 0
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


class UploadManager:
    def __init__(self, settings: Settings, rclone: str, ffprobe: str) -> None:
        self.settings = settings
        self.rclone = rclone
        self.ffprobe = ffprobe
        self.tasks: queue.Queue[Path | None] = queue.Queue()
        self.queued: set[Path] = set()
        # Chunks put back onto ``tasks`` directly (outside of ``enqueue()``) and
        # therefore still "in flight" even though they are momentarily absent
        # from ``retry_pending``. Used by ``_worker``'s ``finally`` block to
        # decide whether ``queued`` can be cleared, without reaching into
        # ``queue.Queue``'s private internals.
        self.requeued: set[Path] = set()
        self.lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.retry_attempts: dict[Path, int] = {}
        self.retry_pending: set[Path] = set()
        self.retry_timers: set[threading.Timer] = set()
        self.failed: set[Path] = set()
        self.invalid_checks: dict[Path, int] = {}

    def start(self) -> None:
        for index in range(self.settings.upload_workers):
            thread = threading.Thread(target=self._worker, name=f"uploader-{index + 1}", daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self) -> None:
        with self.lock:
            timers = list(self.retry_timers)
        for timer in timers:
            timer.cancel()
        for _ in self.threads:
            self.tasks.put(None)
        for thread in self.threads:
            thread.join(timeout=30)

    def enqueue(self, path: Path) -> None:
        path = path.resolve()
        with self.lock:
            if path in self.queued or path in self.failed or self._was_uploaded(path):
                return
            self.queued.add(path)
        self.tasks.put(path)

    def _schedule_retry(self, chunk: Path, returncode: int | None = None) -> None:
        with self.lock:
            failures = self.retry_attempts.get(chunk, 0) + 1
            self.retry_attempts[chunk] = failures
            if failures >= self.settings.processor_max_attempts:
                self.failed.add(chunk)
                self.retry_pending.discard(chunk)
                LOG.critical(
                    "StreamSlice failed permanently for %s after %s attempts; "
                    "the chunk is preserved and later chunks will continue",
                    chunk,
                    failures,
                )
                return
            delay = min(
                self.settings.processor_retry_initial_seconds * (2 ** (failures - 1)),
                self.settings.processor_retry_max_seconds,
            )
            self.retry_pending.add(chunk)

        LOG.error(
            "StreamSlice failed for %s (exit %s, attempt %s/%s); retrying in %ss",
            chunk,
            returncode if returncode is not None else "exception",
            failures,
            self.settings.processor_max_attempts,
            delay,
        )

        def requeue() -> None:
            # Discarding from retry_pending and re-queuing must happen under the
            # same lock: releasing the lock in between would leave a window
            # where a concurrent directory scan sees the chunk as no longer
            # pending and enqueues it a second time.
            with self.lock:
                self.retry_pending.discard(chunk)
                self.retry_timers.discard(timer)
                should_requeue = (
                    chunk.exists()
                    and chunk not in self.failed
                    and not self._was_uploaded(chunk)
                )
                if should_requeue:
                    self.requeued.add(chunk)
                    self.tasks.put(chunk)

        timer = threading.Timer(delay, requeue)
        timer.daemon = True
        with self.lock:
            self.retry_timers.add(timer)
        timer.start()

    def _state_path(self, chunk: Path) -> Path:
        return chunk.parent / ".uploaded.json"

    def _was_uploaded(self, chunk: Path) -> bool:
        state_path = self._state_path(chunk)
        try:
            return chunk.name in set(json.loads(state_path.read_text(encoding="utf-8")))
        except (OSError, TypeError, json.JSONDecodeError):
            return False

    def _mark_uploaded(self, chunk: Path) -> None:
        state_path = self._state_path(chunk)
        with self.lock:
            try:
                uploaded = set(json.loads(state_path.read_text(encoding="utf-8")))
            except (OSError, TypeError, json.JSONDecodeError):
                uploaded = set()
            uploaded.add(chunk.name)
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(sorted(uploaded), ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(state_path)

    def _remote_path(self, chunk: Path) -> str:
        relative = chunk.relative_to(self.settings.download_directory).as_posix()
        prefix = f"{self.settings.rclone_base_path}/" if self.settings.rclone_base_path else ""
        return f"{self.settings.rclone_remote}:{prefix}{relative}"

    def _requeue_direct(self, chunk: Path) -> None:
        """Put a chunk back onto the task queue outside of enqueue()."""
        with self.lock:
            self.requeued.add(chunk)
        self.tasks.put(chunk)

    def _worker(self) -> None:
        while True:
            chunk = self.tasks.get()
            if chunk is None:
                self.tasks.task_done()
                return
            with self.lock:
                self.requeued.discard(chunk)
            try:
                if not valid_mp4(chunk, self.ffprobe):
                    with self.lock:
                        seen = self.invalid_checks.get(chunk, 0) + 1
                        self.invalid_checks[chunk] = seen
                        quarantined = seen >= INVALID_QUARANTINE_THRESHOLD
                        if quarantined:
                            self.failed.add(chunk)
                            self.invalid_checks.pop(chunk, None)
                    if quarantined:
                        LOG.critical(
                            "Quarantining invalid/incomplete MP4 after %s checks; it will "
                            "not be retried and will not block later chunks: %s",
                            INVALID_QUARANTINE_THRESHOLD,
                            chunk,
                        )
                    else:
                        LOG.warning(
                            "Invalid/incomplete MP4 (check %s/%s), will re-check later: %s",
                            seen,
                            INVALID_QUARANTINE_THRESHOLD,
                            chunk,
                        )
                    continue
                if self.settings.processor_command:
                    command = [*self.settings.processor_command, "--input", str(chunk)]
                    attempt = self.retry_attempts.get(chunk, 0) + 1
                    LOG.info(
                        "Handing %s to StreamSlice (attempt %s/%s)",
                        chunk,
                        attempt,
                        self.settings.processor_max_attempts,
                    )
                    completed = subprocess.run(
                        command,
                        check=False,
                        timeout=self.settings.processor_timeout_seconds,
                    )
                    if completed.returncode != 0:
                        self._schedule_retry(chunk, completed.returncode)
                        continue
                    self._mark_uploaded(chunk)
                    with self.lock:
                        self.retry_attempts.pop(chunk, None)
                        self.retry_pending.discard(chunk)
                    LOG.info("StreamSlice completed and uploaded render bundle for %s", chunk)
                    if self.settings.delete_after_upload:
                        chunk.unlink(missing_ok=True)
                    continue
                command = [
                    self.rclone,
                    "copyto",
                    str(chunk),
                    self._remote_path(chunk),
                    "--retries",
                    "8",
                    "--low-level-retries",
                    "20",
                    "--retries-sleep",
                    "10s",
                    "--stats-one-line",
                ]
                if self.settings.rclone_config:
                    command.extend(["--config", self.settings.rclone_config])
                LOG.info("Uploading %s", chunk)
                completed = subprocess.run(command, check=False)
                if completed.returncode != 0:
                    LOG.error("rclone failed for %s (exit %s); it will be retried", chunk, completed.returncode)
                    time.sleep(30)
                    self._requeue_direct(chunk)
                    continue
                self._mark_uploaded(chunk)
                LOG.info("Uploaded %s -> %s", chunk, self._remote_path(chunk))
                if self.settings.delete_after_upload:
                    chunk.unlink(missing_ok=True)
            except (subprocess.SubprocessError, OSError) as exc:
                LOG.error("Failed to run processor/rclone for %s: %s", chunk, exc)
                if self.settings.processor_command:
                    self._schedule_retry(chunk)
                else:
                    time.sleep(30)
                    self._requeue_direct(chunk)
                continue
            except Exception:
                LOG.exception("Unexpected error while processing %s; retrying", chunk)
                if self.settings.processor_command:
                    self._schedule_retry(chunk)
                else:
                    time.sleep(30)
                    self._requeue_direct(chunk)
                continue
            finally:
                with self.lock:
                    if chunk not in self.retry_pending and chunk not in self.requeued:
                        self.queued.discard(chunk)
                self.tasks.task_done()


class Recorder:
    def __init__(
        self,
        settings: Settings,
        channel: str,
        stream: dict[str, str],
        uploader: UploadManager,
        streamlink: str,
        ffmpeg: str,
        ffprobe: str,
    ) -> None:
        self.settings = settings
        self.channel = channel
        self.stream = stream
        self.uploader = uploader
        self.streamlink = streamlink
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.folder = stream_folder(settings.download_directory, channel, stream["started_at"])
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name=f"recorder-{channel}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _enqueue_completed(self, include_latest: bool) -> None:
        chunks = sorted(
            (path for path in self.folder.glob("chunk_*.mp4") if chunk_number(path) is not None),
            key=lambda path: chunk_number(path) or 0,
        )
        if not include_latest and chunks:
            chunks = chunks[:-1]
        for chunk in chunks:
            self.uploader.enqueue(chunk)

    def run(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.folder).free
        if free < MIN_FREE_BYTES:
            LOG.error(
                "[%s] Only %.1f GiB free at %s; pausing recording until space is "
                "freed (no chunk started)",
                self.channel,
                free / 1024**3,
                self.folder,
            )
            return
        first = next_chunk_number(self.folder)
        streamlink_command = [
            self.streamlink,
            f"https://www.twitch.tv/{self.channel}",
            self.settings.quality,
            "--stream-sorting-excludes",
            f">{self.settings.max_quality}",
            "--stdout",
            "--retry-streams",
            "5",
            "--retry-max",
            "12",
        ]
        if self.settings.oauth_token:
            streamlink_command.extend(
                ["--twitch-api-header", f"Authorization=OAuth {self.settings.oauth_token}"]
            )
        ffmpeg_command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            "pipe:0",
            "-map",
            "0",
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_format",
            "mp4",
            "-segment_time",
            str(self.settings.chunk_duration),
            "-segment_start_number",
            str(first),
            "-reset_timestamps",
            "1",
            "-segment_format_options",
            "movflags=+faststart",
            str(self.folder / "chunk_%d.mp4"),
        ]
        LOG.info("[%s] Recording stream %s into %s (starting chunk %s)", self.channel, self.stream["id"], self.folder, first)
        with (self.folder / "streamlink.log").open("ab") as streamlink_log, (self.folder / "ffmpeg.log").open("ab") as ffmpeg_log:
            stream_process = subprocess.Popen(streamlink_command, stdout=subprocess.PIPE, stderr=streamlink_log)
            assert stream_process.stdout is not None
            ffmpeg_process = subprocess.Popen(ffmpeg_command, stdin=stream_process.stdout, stdout=ffmpeg_log, stderr=ffmpeg_log)
            stream_process.stdout.close()
            while not self.stop_event.wait(10):
                self._enqueue_completed(include_latest=False)
                if shutil.disk_usage(self.folder).free < MIN_FREE_BYTES:
                    LOG.error(
                        "[%s] Low disk (< %.1f GiB free) during recording; stopping to "
                        "finalize the current chunk instead of letting ffmpeg fail",
                        self.channel,
                        MIN_FREE_BYTES / 1024**3,
                    )
                    break
                if stream_process.poll() is not None or ffmpeg_process.poll() is not None:
                    break
            if stream_process.poll() is None:
                stream_process.terminate()
                try:
                    stream_process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    stream_process.kill()
            try:
                ffmpeg_process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                ffmpeg_process.send_signal(signal.SIGINT)
                try:
                    ffmpeg_process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    ffmpeg_process.kill()
                    try:
                        ffmpeg_process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        LOG.error(
                            "[%s] ffmpeg process %s did not terminate after kill()",
                            self.channel,
                            ffmpeg_process.pid,
                        )
            self._enqueue_completed(include_latest=True)
        LOG.info("[%s] Recording stopped", self.channel)


class App:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.streamlink = require_binary("streamlink")
        self.ffmpeg = require_binary("ffmpeg")
        self.ffprobe = require_binary("ffprobe")
        self.rclone = require_binary("rclone")
        self.helix = TwitchHelix(settings)
        self.uploader = UploadManager(settings, self.rclone, self.ffprobe)
        self.recorders: dict[str, Recorder] = {}
        self.shutdown = threading.Event()

    def request_stop(self, *_: object) -> None:
        self.shutdown.set()

    def _recover_chunks(self) -> None:
        for chunk in self.settings.download_directory.glob("*/*/chunk_*.mp4"):
            self.uploader.enqueue(chunk)

    def run(self, once: bool = False) -> None:
        self.settings.download_directory.mkdir(parents=True, exist_ok=True)
        self.uploader.start()
        self._recover_chunks()
        LOG.info("Monitoring: %s", ", ".join(self.settings.channels))
        try:
            while not self.shutdown.is_set():
                for channel, recorder in list(self.recorders.items()):
                    if not recorder.thread.is_alive():
                        recorder.thread.join()
                        del self.recorders[channel]
                try:
                    live = self.helix.live_streams()
                    for channel, stream in live.items():
                        recorder = self.recorders.get(channel)
                        if recorder is None or recorder.stream["id"] != stream["id"]:
                            if recorder is not None:
                                recorder.stop()
                                recorder.thread.join(timeout=60)
                            recorder = Recorder(
                                self.settings, channel, stream, self.uploader, self.streamlink, self.ffmpeg, self.ffprobe
                            )
                            self.recorders[channel] = recorder
                            recorder.start()
                    for channel in set(self.recorders) - set(live):
                        self.recorders[channel].stop()
                except requests.RequestException:
                    LOG.exception("Twitch API request failed; active recordings are left running")
                if once or self.shutdown.wait(self.settings.check_interval):
                    break
        finally:
            for recorder in self.recorders.values():
                recorder.stop()
            for recorder in self.recorders.values():
                recorder.thread.join(timeout=60)
            self.uploader.stop()


def require_binary(name: str) -> str:
    venv_binary = Path(sys.executable).parent / name
    path = str(venv_binary) if venv_binary.is_file() else shutil.which(name)
    if not path:
        raise RuntimeError(f"required executable is not installed: {name}")
    return path


def mask_secret(value: str) -> str:
    """Mask a secret for display: reveal only its length and last 4 characters."""
    if not value:
        return "<empty>"
    tail = value[-4:] if len(value) > 4 else value
    return f"<{len(value)} chars, ending in '{tail}'>"


def check_config(settings: Settings) -> int:
    """Validate configuration and dependencies without touching disk or network."""
    print("Twitch credentials:")
    print(f"  client_id:     {mask_secret(settings.client_id)}")
    print(f"  client_secret: {mask_secret(settings.client_secret)}")
    print(f"  oauth_token:   {mask_secret(settings.oauth_token)}")
    print("Channels:", ", ".join(settings.channels))
    print("Download directory:", settings.download_directory)
    print(f"Check interval: {settings.check_interval}s")
    print(f"Chunk duration: {settings.chunk_duration}s")
    print(f"Quality: {settings.quality} (max {settings.max_quality})")
    if settings.rclone_remote:
        print(
            f"rclone: remote={settings.rclone_remote}:, base_path={settings.rclone_base_path}, "
            f"delete_after_upload={settings.delete_after_upload}, workers={settings.upload_workers}, "
            f"config={settings.rclone_config or '<default>'}"
        )
    else:
        print("rclone: not configured")
    if settings.processor_command:
        print(
            "processor: command=" + " ".join(settings.processor_command) + " | "
            f"max_attempts={settings.processor_max_attempts}, "
            f"retry={settings.processor_retry_initial_seconds}.."
            f"{settings.processor_retry_max_seconds}s, "
            f"timeout={settings.processor_timeout_seconds}s"
        )
    else:
        print("processor: not configured (chunks are uploaded via rclone directly)")

    missing = []
    for binary in ("streamlink", "ffmpeg", "ffprobe", "rclone"):
        try:
            path = require_binary(binary)
        except RuntimeError:
            missing.append(binary)
            print(f"  {binary}: NOT FOUND")
        else:
            print(f"  {binary}: {path}")

    if missing:
        LOG.error("Missing required executables: %s", ", ".join(missing))
        return 1
    LOG.info("Configuration and dependencies are valid")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--once", action="store_true", help="poll Twitch once (useful for diagnostics)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
    try:
        settings = Settings.load(args.config.resolve())
        if args.check_config:
            return check_config(settings)
        app = App(settings)
        signal.signal(signal.SIGTERM, app.request_stop)
        signal.signal(signal.SIGINT, app.request_stop)
        app.run(once=args.once)
        return 0
    except Exception:
        LOG.exception("Fatal error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
