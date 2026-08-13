"""
Video downloader module using yt-dlp.

This module provides functionality to download videos and audio from various platforms.
"""

import asyncio
import json
import os
import re
import shutil
import sys
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from urllib.parse import urlparse

import httpx

from astrbot.api import logger

KEMONO_HOSTS = {"kemono.su", "kemono.cr", "kemono.party"}
YT_DLP_JS_RUNTIMES = "node"
YT_DLP_REMOTE_COMPONENTS = "ejs:github"
READLINE_TIMEOUT = 180.0
TOTAL_TIMEOUT = 1200.0
MAX_IDENTICAL_OUTPUT = 5
YT_DLP_SOCKET_TIMEOUT = "60"
YT_DLP_RETRIES = "10"
YT_DLP_EXTRACTOR_RETRIES = "5"
YT_DLP_FRAGMENT_RETRIES = "20"


def build_yt_dlp_base_command() -> list[str]:
    """Build shared yt-dlp arguments required by this plugin."""
    environment_executable = Path(sys.executable).with_name("yt-dlp")
    executable = (
        str(environment_executable)
        if environment_executable.is_file()
        else shutil.which("yt-dlp") or "yt-dlp"
    )
    return [
        executable,
        "--js-runtimes",
        YT_DLP_JS_RUNTIMES,
        "--remote-components",
        YT_DLP_REMOTE_COMPONENTS,
        "--socket-timeout",
        YT_DLP_SOCKET_TIMEOUT,
        "--retries",
        YT_DLP_RETRIES,
        "--extractor-retries",
        YT_DLP_EXTRACTOR_RETRIES,
        "--fragment-retries",
        YT_DLP_FRAGMENT_RETRIES,
        "--continue",
    ]


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

    # yt-dlp may omit speed or ETA, especially near completion.
    progress_pattern = re.compile(
        r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+~?\s*(\S+)"
        r"(?:\s+at\s+(\S+))?(?:\s+ETA\s+(\S+))?"
    )
    match = progress_pattern.search(line)
    if match:
        percent = match.group(1)
        details = [f"Progress: {percent}%", f"Size: {match.group(2)}"]
        if match.group(3):
            details.append(f"Speed: {match.group(3)}")
        if match.group(4):
            details.append(f"ETA: {match.group(4)}")
        return " | ".join(details)

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
    on_stop: Callable[[], bool] | None = None,
    readline_timeout: float = READLINE_TIMEOUT,
    total_timeout: float = TOTAL_TIMEOUT,
    cookie_browser: str = "",
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
        on_stop: Optional callback returning True when the caller requested cancellation; the subprocess is terminated and a failure is yielded.
        readline_timeout: Maximum idle seconds between output lines before the subprocess is terminated.
        total_timeout: Maximum total wall-clock seconds before the subprocess is terminated.
        cookie_browser: yt-dlp browser cookie source, such as ``chrome`` or
            ``firefox:default-release``.

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
        "--progress",
        "--extractor-args",
        "youtube:lang=zh-CN",
        "-o",
        output_template,
        "--embed-thumbnail",
        "--no-mtime",
        "-i",
        "--add-metadata",
    ]

    cookie_browser = cookie_browser.strip()
    if cookie_browser:
        command.extend(["--cookies-from-browser", cookie_browser])
        cookie_source = f"browser:{cookie_browser}"
    elif cookie_file:
        command.extend(["--cookies", cookie_file])
        cookie_source = f"file:{cookie_file}"
    else:
        cookie_source = "none"

    if cookie_file and not Path(cookie_file).is_file() and not cookie_browser:
        logger.warning("yt-dlp cookie file does not exist: %s", cookie_file)

    # Make the plugin setting override inherited HTTP(S)_PROXY variables.
    command.extend(["--proxy", proxy_url])

    if audio:
        command.append("-x")

    if enable_archive:
        Path(archive_path).parent.mkdir(parents=True, exist_ok=True)
        command.extend(["--download-archive", archive_path])

    if "pornhub.com" in link:
        command.extend(["--referer", "https://www.pornhub.com/"])

    if logger.isEnabledFor(10):
        command.append("--verbose")

    command.append(link)

    proxy_for_log = proxy_url
    if proxy_url:
        parsed_proxy = urlparse(proxy_url)
        if parsed_proxy.username or parsed_proxy.password:
            host = parsed_proxy.hostname or ""
            port = f":{parsed_proxy.port}" if parsed_proxy.port else ""
            proxy_for_log = f"{parsed_proxy.scheme}://***@{host}{port}"
    logged_command = list(command)
    for option in ("--cookies", "--cookies-from-browser", "--proxy"):
        if option in logged_command:
            option_index = logged_command.index(option)
            if option == "--cookies":
                logged_command[option_index + 1] = "<cookie-file>"
            elif option == "--cookies-from-browser":
                logged_command[option_index + 1] = "<browser-cookie-source>"
            else:
                logged_command[option_index + 1] = proxy_for_log
    logger.info(
        "Starting yt-dlp: executable=%s cookie_source=%s proxy=%s url=%s",
        command[0],
        cookie_source,
        proxy_for_log or "none",
        link,
    )
    logger.debug("yt-dlp command: %s", logged_command)

    last_yield_time = 0.0

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logger.exception("Unable to start yt-dlp: executable=%s", command[0])
        yield ("failed", f"Unable to start yt-dlp ({command[0]}): {exc}")
        return

    logger.info("yt-dlp started: pid=%s", process.pid)

    stderr_lines: list[str] = []
    error_lines: list[str] = []
    stdout_closed = False
    stderr_closed = False
    failed = False
    start_time = time.monotonic()

    while not (stdout_closed and stderr_closed):
        if on_stop is not None and on_stop():
            logger.info("Stopping yt-dlp after cancellation: pid=%s", process.pid)
            await _terminate_process(process)
            yield ("failed", "下载已取消")
            return
        elapsed = time.monotonic() - start_time
        if elapsed >= total_timeout:
            logger.warning(
                "Stopping yt-dlp after total timeout: pid=%s timeout=%ss",
                process.pid,
                int(total_timeout),
            )
            await _terminate_process(process)
            yield ("failed", f"下载总时长超过 {int(total_timeout)}s，已终止")
            return

        tasks: dict[asyncio.Task[bytes], str] = {}
        if not stdout_closed:
            tasks[asyncio.create_task(process.stdout.readline())] = "stdout"
        if not stderr_closed:
            tasks[asyncio.create_task(process.stderr.readline())] = "stderr"

        try:
            done, pending = await asyncio.wait(
                tasks,
                timeout=readline_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.TimeoutError:
            done, pending = set(), set(tasks)

        if not done:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.warning(
                "Stopping yt-dlp after idle timeout: pid=%s timeout=%ss",
                process.pid,
                int(readline_timeout),
            )
            await _terminate_process(process)
            yield (
                "failed",
                f"yt-dlp 超过 {int(readline_timeout)}s 无输出，已终止",
            )
            return

        for task in done:
            stream = tasks[task]
            output = await task
            if not output:
                if stream == "stdout":
                    stdout_closed = True
                else:
                    stderr_closed = True
                continue

            now = time.monotonic()

            decoded_output = output.decode("utf-8", errors="replace").strip()
            safe_output = re.sub(
                r"https?://[^\s\"']+\?[^\s\"']+",
                lambda match: match.group(0).split("?", 1)[0] + "?<redacted>",
                decoded_output,
            )
            if proxy_url:
                safe_output = safe_output.replace(proxy_url, proxy_for_log)
            formatted = format_ytdlp_progress(decoded_output)
            logger.debug("yt-dlp (%s): %s", stream, safe_output)

            if stream == "stdout":
                if os.path.exists(decoded_output):
                    yield ("save_path", decoded_output)
                elif formatted and (now - last_yield_time >= interval):
                    last_yield_time = now
                    yield ("progress", formatted)

            else:
                if decoded_output:
                    stderr_lines.append(safe_output)
                    if len(stderr_lines) > 40:
                        del stderr_lines[:-40]
                    if decoded_output.startswith("ERROR:") or "ERROR" in decoded_output:
                        error_lines.append(safe_output)
                        if len(error_lines) > 10:
                            del error_lines[:-10]
                if formatted:
                    if now - last_yield_time >= interval:
                        last_yield_time = now
                        yield ("progress", formatted)
                    continue
                if "ERROR" in decoded_output:
                    failed = True

        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    if failed and process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError:
        await _terminate_process(process)
    logger.info(
        "yt-dlp finished: pid=%s returncode=%s failed=%s",
        process.pid,
        process.returncode,
        failed,
    )
    if process.returncode == 0:
        yield ("success", "")
    elif not failed:
        error_msg = "\n".join(error_lines[-5:] or stderr_lines[-5:])
        yield (
            "failed",
            error_msg or f"yt-dlp exited with code {process.returncode}",
        )
    else:
        error_msg = "\n".join(error_lines[-5:] or stderr_lines[-5:])
        yield (
            "failed",
            error_msg or f"yt-dlp exited with code {process.returncode}",
        )


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


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate a subprocess, escalating to kill if it refuses to exit.

    Args:
        process: The subprocess to terminate.
    """
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await asyncio.gather(process.wait(), return_exceptions=True)


async def _stream_process_output(
    command: list[str],
    *,
    cwd: Path | None = None,
    on_stop: Callable[[], bool] | None = None,
    readline_timeout: float = READLINE_TIMEOUT,
    total_timeout: float = TOTAL_TIMEOUT,
) -> AsyncGenerator[tuple[str, str], None]:
    """Run a subprocess and stream stdout/stderr lines.

    Args:
        command: Command and arguments to execute.
        cwd: Optional working directory for the subprocess.
        on_stop: Optional callback returning True when the caller requested cancellation; the subprocess is terminated and a failure is yielded.
        readline_timeout: Maximum idle seconds between output lines before the subprocess is terminated.
        total_timeout: Maximum total wall-clock seconds before the subprocess is terminated.

    Yields:
        Tuples of (status, data) where status is "output", "success", or "failed".
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd) if cwd else None,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        yield ("failed", f"Command not found: {command[0]}")
        return

    logger.info(
        "Started downloader subprocess: command=%s pid=%s", command[0], process.pid
    )
    assert process.stdout is not None
    last_output = ""
    identical_count = 0
    start_time = time.monotonic()
    try:
        while True:
            if on_stop is not None and on_stop():
                await _terminate_process(process)
                yield ("failed", "下载已取消")
                return
            elapsed = time.monotonic() - start_time
            if elapsed >= total_timeout:
                await _terminate_process(process)
                yield (
                    "failed",
                    f"下载总时长超过 {int(total_timeout)}s，已终止",
                )
                return
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=readline_timeout,
                )
            except TimeoutError:
                await _terminate_process(process)
                yield (
                    "failed",
                    f"{command[0]} 超过 {int(readline_timeout)}s 无输出，已终止",
                )
                return
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if not decoded:
                continue
            logger.debug("%s output: %s", command[0], decoded)
            # gallery-dl / ktoolbox 会在鉴权失败等情况时反复打印同一行错误并
            # 持续重试，导致 readline 不会触发空闲超时；检测到同一行连续重复
            # 即判定为卡在重试循环，主动终止子进程。
            if decoded == last_output:
                identical_count += 1
                if identical_count >= MAX_IDENTICAL_OUTPUT:
                    await _terminate_process(process)
                    yield (
                        "failed",
                        f"{command[0]} 持续重复输出同一行，疑似卡在重试循环，已终止",
                    )
                    return
            else:
                last_output = decoded
                identical_count = 0
            yield ("output", decoded)

        await asyncio.wait_for(process.wait(), timeout=5.0)
    except (TimeoutError, asyncio.TimeoutError):
        await _terminate_process(process)
        yield ("failed", f"{command[0]} 等待退出超时，已终止")
        return

    if process.returncode == 0:
        logger.info(
            "Downloader subprocess finished: command=%s pid=%s returncode=0",
            command[0],
            process.pid,
        )
        yield ("success", "")
    else:
        logger.warning(
            "Downloader subprocess failed: command=%s pid=%s returncode=%s",
            command[0],
            process.pid,
            process.returncode,
        )
        yield ("failed", f"Command exited with code {process.returncode}")


async def download_with_gallery_dl(
    link: str,
    output_dir: Path,
    config_file: str = "",
    cookie_file: str = "",
    proxy_url: str = "",
    enable_archive: bool = True,
    archive_path: str = "data/archive-gallery.txt",
    on_stop: Callable[[], bool] | None = None,
) -> AsyncGenerator[tuple[str, str], None]:
    """Download images using gallery-dl.

    Args:
        link: Image gallery URL.
        output_dir: Directory to download into.
        config_file: Optional gallery-dl config file path.
        cookie_file: Optional gallery-dl cookies file path.
        proxy_url: Optional proxy URL.
        enable_archive: Whether to enable download archive.
        archive_path: Path to the archive file.
        on_stop: Optional callback returning True when the caller requested cancellation.
    """
    command = [
        "gallery-dl",
        "--config-ignore",
        "--abort",
        "1",
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

    async for state_type, data in _stream_process_output(command, on_stop=on_stop):
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
    on_stop: Callable[[], bool] | None = None,
) -> AsyncGenerator[tuple[str, str], None]:
    """Download images using ktoolbox.

    Args:
        link: Kemono URL.
        workspace: Working directory for the ktoolbox subprocess.
        output_dir: Directory to download into.
        on_stop: Optional callback returning True when the caller requested cancellation.
    """
    command = infer_ktoolbox_command(link)
    command.append(str(output_dir))

    async for state_type, data in _stream_process_output(
        command, cwd=workspace, on_stop=on_stop
    ):
        if state_type == "output":
            yield ("progress", data)
        else:
            yield (state_type, data)
