"""
Video downloader module using yt-dlp.

This module provides functionality to download videos and audio from various platforms.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("astrbot")

KEMONO_HOSTS = {"kemono.su", "kemono.cr", "kemono.party"}
YT_DLP_JS_RUNTIMES = "node"


def build_yt_dlp_base_command() -> list[str]:
    """Build shared yt-dlp arguments required by this plugin."""
    return ["yt-dlp", "--js-runtimes", YT_DLP_JS_RUNTIMES]


async def download_file(url: str, save_path: Path) -> tuple[bool, float]:
    """Download a file from URL to the specified path.

    Args:
        url: URL to download from, or local file path
        save_path: Path to save the file

    Returns:
        Tuple of (success, file_size_in_mb)
    """
    if Path(url).exists():
        shutil.copy(url, save_path)
        return True, save_path.stat().st_size / 1024 / 1024

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url, follow_redirects=True) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("Content-Length", 0))
                    with open(save_path, "wb") as f:
                        async for chunk in r.aiter_bytes():
                            if chunk:
                                f.write(chunk)
                                await asyncio.sleep(0)

            return True, total / 1024 / 1024

        except Exception as e:
            if attempt < 2:
                logger.error(f"Error downloading {save_path}: {e}. Retrying...")
                await asyncio.sleep(2)
            else:
                logger.error(f"Failed to download {save_path}: {e}")
                return False, 0

    return False, 0


async def get_video_title(link: str) -> tuple[str, str]:
    """Get the title of a video from its URL.

    Args:
        link: Video URL

    Returns:
        Tuple of (status, title_or_error)
    """
    cmd = [*build_yt_dlp_base_command(), "--get-title", link]

    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await process.communicate()

    if process.returncode == 0:
        title = stdout.decode("utf-8", errors="replace").strip()
        return "success", title
    else:
        error_message = stderr.decode().strip()
        logger.error(f"Failed to get video title with error: {error_message}")
        return "failed", error_message


def format_ytdlp_progress(line: str) -> str | None:
    """Parse yt-dlp progress output and format it.

    Args:
        line: A single line of yt-dlp output

    Returns:
        Formatted progress string or None if not a progress line
    """
    if not (
        line.strip().startswith("[download]") or line.strip().startswith("[Metadata]")
    ):
        return None

    # Progress line pattern: percentage, total size, speed, ETA
    progress_pattern = re.compile(
        r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+[\s~]*([\d.]+(?:[KMGT]i?B)?)\s+at\s+([\d.]+\w+/s)\s+ETA\s+(\d+:\d+)"
    )
    match = progress_pattern.search(line)
    if match:
        percent = match.group(1)
        total_size = match.group(2).replace("~", "")
        speed = match.group(3)
        eta = match.group(4)
        return (
            f"Progress: {percent}% | Size: {total_size} | Speed: {speed} | ETA: {eta}"
        )

    # Metadata pattern
    metadata_pattern = re.compile(r'\[Metadata\]\sAdding\smetadata\sto\s"(.*)"')
    metadata_match = metadata_pattern.search(line)
    if metadata_match:
        save_path = metadata_match.group(1)
        return f"✅ Download complete | Saved to: {save_path}"

    return None


async def download_with_yt_dlp(
    link: str,
    output_template: str,
    cookie_file: str,
    proxy_url: str = "",
    audio: bool = False,
    enable_archive: bool = True,
    archive_path: str = "data/archive.txt",
    interval: float = 2.0,
) -> AsyncGenerator[tuple[str, str], None]:
    """Download video/audio using yt-dlp.

    Args:
        link: Video URL
        output_template: Output file path template
        cookie_file: Path to cookies file
        proxy_url: Proxy URL (empty string for no proxy)
        audio: Whether to download audio only
        enable_archive: Whether to enable download archive
        archive_path: Path to archive file
        interval: Minimum interval between progress updates

    Yields:
        Tuple of (status, data) where status can be:
        - "progress": Progress update
        - "save_path": Download complete with file path
        - "success": Download complete (title returned)
        - "failed": Download failed with error message
    """
    command = [
        *build_yt_dlp_base_command(),
        "--print",
        "after_move:filepath",
        "--newline",
        "--extractor-args",
        "youtube:lang=zh-CN",
        "-o",
        output_template,
        "--embed-thumbnail",
        "--no-mtime",
        "-i",
        "--add-metadata",
        link,
    ]

    if cookie_file:
        command.extend(["--cookies", cookie_file])

    if proxy_url:
        command.extend(["--proxy", proxy_url])

    if audio:
        command.append("-x")

    if enable_archive:
        Path(archive_path).parent.mkdir(parents=True, exist_ok=True)
        command.extend(["--download-archive", archive_path])

    if "pornhub.com" in link:
        command.extend(["--referer", "https://www.pornhub.com/"])

    _, title = await get_video_title(link)
    last_yield_time = 0.0

    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    error_msg = None

    while True:
        stdout_task = asyncio.create_task(process.stdout.readline())
        stderr_task = asyncio.create_task(process.stderr.readline())

        done, pending = await asyncio.wait(
            {stdout_task, stderr_task}, return_when=asyncio.FIRST_COMPLETED
        )

        failed = False
        success = False

        for task in done:
            output = await task
            if not output:
                continue

            now = time.monotonic()

            if task is stdout_task:
                decoded_output = output.decode("utf-8", errors="replace").strip()
                formatted = format_ytdlp_progress(decoded_output)
                logger.debug(f"yt-dlp: {decoded_output}\n{formatted}")

                if os.path.exists(decoded_output):
                    yield ("save_path", decoded_output)
                    success = True
                elif formatted and (now - last_yield_time >= interval):
                    last_yield_time = now
                    yield ("progress", formatted)

            elif task is stderr_task:
                error_msg = output.decode().strip()
                if "ERROR" in error_msg:
                    failed = True
                    yield (
                        "failed",
                        f"Download failed: {error_msg}"
                        if error_msg
                        else "Download failed",
                    )
                    break

        if failed:
            break

        if process.returncode is not None:
            if process.returncode == 0 and not success:
                yield ("success", title)
                break

        for task in pending:
            task.cancel()

    await process.wait()


def determine_filename(text_content: str, file_urls: list[str]) -> str:
    """Determine the filename with appropriate extension.

    Args:
        text_content: Provided filename text
        file_urls: List of file URLs

    Returns:
        Filename with extension
    """
    if text_content:
        filename = (
            re.sub(r"[^\w\s-]", "", text_content.strip()).strip().replace(" ", "_")[:50]
        )
        if not filename:
            filename = f"download_{int(time.time())}"
    else:
        filename = f"download_{int(time.time())}"

    video_extensions = [".mp4", ".avi", ".mov", ".wmv", ".flv", ".mkv", ".webm"]
    audio_extensions = [".mp3", ".wav", ".flac", ".aac", ".ogg"]

    if any(url.lower().endswith(ext) for url in file_urls for ext in video_extensions):
        if not filename.lower().endswith(tuple(video_extensions)):
            filename += ".mp4"
    elif any(
        url.lower().endswith(ext) for url in file_urls for ext in audio_extensions
    ):
        if not filename.lower().endswith(tuple(audio_extensions)):
            filename += ".mp3"
    else:
        if "." not in filename:
            filename += ".file"

    return filename


def is_ktoolbox_url(url: str) -> bool:
    """Return whether the URL should be handled by ktoolbox."""
    hostname = urlparse(url).hostname or ""
    return hostname.lower() in KEMONO_HOSTS


def infer_ktoolbox_command(url: str) -> list[str]:
    """Infer the appropriate ktoolbox subcommand from the URL."""
    path = urlparse(url).path
    if "/post/" in path:
        return ["ktoolbox", "download-post", url]
    return ["ktoolbox", "sync-creator", url]


def extract_session_key_from_cookie_file(cookie_file: str) -> str:
    """Extract ktoolbox session key from a cookie export file."""
    if not cookie_file:
        return ""

    path = Path(cookie_file)
    if not path.is_file():
        return ""

    raw_text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw_text:
        return ""

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict):
        cookie_items = payload.get("cookies")
        if isinstance(cookie_items, list):
            for item in cookie_items:
                if isinstance(item, dict) and item.get("name") == "session":
                    return str(item.get("value", ""))
        elif payload.get("name") == "session":
            return str(payload.get("value", ""))

    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and item.get("name") == "session":
                return str(item.get("value", ""))

    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if "\t" in stripped:
            parts = stripped.split("\t")
            if len(parts) >= 7 and parts[5] == "session":
                return parts[6]

        if stripped.startswith("session="):
            return stripped.split("=", 1)[1]

    return ""


async def _stream_process_output(
    command: list[str],
    *,
    cwd: Path | None = None,
) -> AsyncGenerator[tuple[str, str], None]:
    """Run a subprocess and stream stdout/stderr lines."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        yield ("failed", f"Command not found: {command[0]}")
        return

    assert process.stdout is not None
    while True:
        line = await process.stdout.readline()
        if not line:
            break
        decoded = line.decode("utf-8", errors="replace").strip()
        if decoded:
            yield ("output", decoded)

    await process.wait()
    if process.returncode == 0:
        yield ("success", "")
    else:
        yield ("failed", f"Command exited with code {process.returncode}")


async def download_with_gallery_dl(
    link: str,
    output_dir: Path,
    config_file: str = "",
    cookie_file: str = "",
    proxy_url: str = "",
    enable_archive: bool = True,
    archive_path: str = "data/archive-gallery.txt",
) -> AsyncGenerator[tuple[str, str], None]:
    """Download images using gallery-dl."""
    command = [
        "gallery-dl",
        "--config-ignore",
        "-d",
        str(output_dir),
        "--no-colors",
        "--no-input",
    ]

    if config_file:
        command.extend(["-c", config_file])

    if cookie_file:
        command.extend(["-C", cookie_file])

    if proxy_url:
        command.extend(["--proxy", proxy_url])

    if enable_archive:
        Path(archive_path).parent.mkdir(parents=True, exist_ok=True)
        command.extend(["--download-archive", archive_path])

    command.append(link)

    async for state_type, data in _stream_process_output(command):
        if state_type == "output":
            yield ("progress", data)
        else:
            yield (state_type, data)


def prepare_ktoolbox_env(
    workspace: Path,
    config_file: str = "",
    session_key: str = "",
) -> None:
    """Prepare the working directory files used by ktoolbox configuration."""
    workspace.mkdir(parents=True, exist_ok=True)

    env_path = workspace / ".env"
    lines: list[str] = []

    if config_file:
        config_text = Path(config_file).read_text(encoding="utf-8", errors="replace")
        lines.append(config_text.rstrip())

    if session_key:
        lines.append(f'KTOOLBOX_API__SESSION_KEY="{session_key}"')

    if lines:
        env_path.write_text("\n".join(line for line in lines if line) + "\n")


async def download_with_ktoolbox(
    link: str,
    workspace: Path,
    output_dir: Path,
) -> AsyncGenerator[tuple[str, str], None]:
    """Download images using ktoolbox."""
    command = infer_ktoolbox_command(link)
    command.append(str(output_dir))

    async for state_type, data in _stream_process_output(command, cwd=workspace):
        if state_type == "output":
            yield ("progress", data)
        else:
            yield (state_type, data)
