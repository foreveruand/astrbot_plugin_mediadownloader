"""
Rclone transfer module for remote storage upload.

This module provides functionality to transfer downloaded files to remote storage using rclone.
"""

import asyncio
import re
from collections.abc import AsyncGenerator
from pathlib import Path

from astrbot.api import logger


def format_rclone_progress(line: str) -> str | None:
    """Parse rclone progress output.

    Args:
        line: A single line of rclone output

    Returns:
        Formatted progress string or None
    """
    line = " ".join(line.strip().split())

    transferred_pattern = re.compile(
        r"Transferred:\s*([^,]+?)\s*/\s*([^,]+?),\s*"
        r"(\d+(?:\.\d+)?)%,\s*([^,]+?/s),\s*ETA\s*(.+)",
        re.IGNORECASE,
    )
    match = transferred_pattern.search(line)
    if match:
        done = match.group(1).strip()
        total = match.group(2).strip()
        percent = match.group(3)
        speed = match.group(4).strip()
        eta = match.group(5).strip()
        return (
            f"Transfer: {percent}% | Size: {done} / {total} | "
            f"Speed: {speed} | ETA: {eta}"
        )

    # Match per-file progress like: "50% / 500.000 MiB, 10.000 MiB/s, ETA 0s"
    progress_pattern = re.compile(
        r"\s*(\d+(?:\.\d+)?)%\s*/\s*([^,]+),\s*([^,]+?/s),\s*ETA\s*(.+)"
    )
    match = progress_pattern.search(line)
    if match:
        percent = match.group(1)
        total = match.group(2).strip()
        speed = match.group(3).strip()
        eta = match.group(4).strip()
        return f"Transfer: {percent}% | Size: {total} | Speed: {speed} | ETA: {eta}"

    return None


async def _iter_rclone_output(
    process: asyncio.subprocess.Process,
) -> AsyncGenerator[str, None]:
    """Yield rclone output split by newline or carriage return."""
    assert process.stdout is not None
    buffer = ""

    while True:
        chunk = await process.stdout.read(1024)
        if not chunk:
            break

        buffer += chunk.decode("utf-8", errors="replace")
        while True:
            separators = [
                pos for pos in (buffer.find("\n"), buffer.find("\r")) if pos >= 0
            ]
            if not separators:
                break
            split_at = min(separators)
            record = buffer[:split_at].strip()
            buffer = buffer[split_at + 1 :]
            if record:
                yield record

    if buffer.strip():
        yield buffer.strip()


async def rclone_transfer(
    local_path: str | Path,
    remote_name: str,
    remote_path: str,
    stats_interval: str = "2s",
) -> AsyncGenerator[tuple[str, str], None]:
    """Transfer a file to remote storage using rclone.

    Args:
        local_path: Local file path
        remote_name: rclone remote name
        remote_path: Remote directory path
        stats_interval: Stats update interval

    Yields:
        Tuple of (status, data) where status can be:
        - "progress": Transfer progress update
        - "success": Transfer complete
        - "failed": Transfer failed with error message
    """
    local_path = str(local_path)
    remote_full = f"{remote_name}:{remote_path}/"

    cmd = [
        "rclone",
        "move",
        local_path,
        remote_full,
        "-P",
        "--stats-one-line",
        f"--stats={stats_interval}",
    ]

    logger.info(f"Starting rclone transfer: {local_path} -> {remote_full}")

    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )

    async for decoded in _iter_rclone_output(process):
        formatted = format_rclone_progress(decoded)
        if formatted:
            yield ("progress", formatted)
        elif "ERROR" in decoded.upper():
            yield ("failed", decoded)

    await process.wait()

    if process.returncode == 0:
        yield ("success", f"Transfer complete: {remote_full}{Path(local_path).name}")
    else:
        yield ("failed", f"Transfer failed with code {process.returncode}")


async def rclone_move_directory(
    local_dir: str | Path,
    remote_name: str,
    remote_path: str,
    stats_interval: str = "2s",
) -> AsyncGenerator[tuple[str, str], None]:
    """Move entire directory to remote storage using rclone.

    Args:
        local_dir: Local directory path
        remote_name: rclone remote name
        remote_path: Remote directory path
        stats_interval: Stats update interval

    Yields:
        Tuple of (status, data)
    """
    local_dir = str(local_dir)
    remote_full = f"{remote_name}:{remote_path}/"

    cmd = [
        "rclone",
        "move",
        local_dir,
        remote_full,
        "-P",
        "--stats-one-line",
        f"--stats={stats_interval}",
    ]

    logger.info(f"Starting rclone directory transfer: {local_dir} -> {remote_full}")

    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )

    async for decoded in _iter_rclone_output(process):
        formatted = format_rclone_progress(decoded)
        if formatted:
            yield ("progress", formatted)
        elif "ERROR" in decoded.upper():
            yield ("failed", decoded)

    await process.wait()

    if process.returncode == 0:
        yield ("success", f"Directory transfer complete: {remote_full}")
    else:
        yield ("failed", f"Directory transfer failed with code {process.returncode}")
