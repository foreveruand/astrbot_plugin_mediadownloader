"""
AstrBot Media Downloader Plugin.

This plugin provides video, audio, and image download functionality
with optional rclone upload support.
"""

import asyncio
import logging
import os
import re
import uuid
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.message_components import File, Record, Video
from astrbot.api.util import SessionController, session_waiter
from astrbot.core.platform.sources.telegram.tg_event import TelegramCallbackQueryEvent
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_plugin_data_path,
    get_astrbot_temp_path,
)

from .downloader import (
    determine_filename,
    download_file,
    download_with_gallery_dl,
    download_with_ktoolbox,
    download_with_yt_dlp,
    extract_session_key_from_cookie_file,
    is_ktoolbox_url,
    prepare_ktoolbox_env,
)
from .rclone import rclone_move_directory, rclone_transfer

logger = logging.getLogger("astrbot")

SESSION_STATE: dict[str, dict[str, Any]] = {}
SESSION_TIMEOUT = 300
MAX_RETRIES = 4
NON_TELEGRAM_PROGRESS_INTERVAL = 30.0


class Main(star.Star):
    """Main class for the Media Downloader plugin."""

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.context = context
        self.config = config
        self._initialized = False

    async def _send_stream_updates(
        self,
        event: AstrMessageEvent,
        stream_factory: Callable[[], AsyncGenerator[str, None]],
        *,
        throttle_interval: float = NON_TELEGRAM_PROGRESS_INTERVAL,
    ) -> None:
        """Send progress updates, editing Telegram messages and throttling others."""

        if event.get_platform_name() == "telegram":
            await self._send_telegram_progress_updates(event, stream_factory)
            return

        last = ""
        last_sent = ""
        last_send_time = 0.0
        pending = ""
        loop = asyncio.get_running_loop()

        async for text in stream_factory():
            if not text or text == last:
                continue
            last = text
            pending = text
            now = loop.time()
            if now - last_send_time >= throttle_interval:
                await event.send(event.plain_result(text))
                last_sent = text
                last_send_time = now

        if pending and pending != last_sent:
            await event.send(event.plain_result(pending))

    async def _send_telegram_progress_updates(
        self,
        event: AstrMessageEvent,
        stream_factory: Callable[[], AsyncGenerator[str, None]],
    ) -> None:
        """Update a single Telegram progress message when possible."""
        last = ""
        last_edit_time = 0.0
        throttle_interval = 0.6
        progress_message: Any | None = None
        loop = asyncio.get_running_loop()

        async def edit_or_send(text: str, *, force: bool = False) -> None:
            nonlocal progress_message, last_edit_time
            now = loop.time()
            if not force and now - last_edit_time < throttle_interval:
                return

            if hasattr(event, "_edit_message"):
                await event._edit_message(text)  # noqa: SLF001
                last_edit_time = loop.time()
                return

            client = getattr(event, "client", None)
            if client is None:
                await event.send(event.plain_result(text))
                last_edit_time = loop.time()
                return

            chat_id = event.get_sender_id()
            message_thread_id = None
            if event.get_message_type().name == "GROUP_MESSAGE":
                chat_id = getattr(event.message_obj, "group_id", chat_id)
            if "#" in str(chat_id):
                chat_id, message_thread_id = str(chat_id).split("#", 1)

            payload: dict[str, Any] = {"chat_id": chat_id}
            if message_thread_id:
                payload["message_thread_id"] = message_thread_id

            try:
                if progress_message is None:
                    progress_message = await client.send_message(text=text, **payload)
                else:
                    await client.edit_message_text(
                        text=text,
                        chat_id=payload["chat_id"],
                        message_id=progress_message.message_id,
                    )
            except Exception as exc:  # pragma: no cover - platform specific
                logger.warning(f"Telegram progress edit failed: {exc}")
            last_edit_time = loop.time()

        try:
            async for text in stream_factory():
                if not text or text == last:
                    continue
                last = text
                await edit_or_send(text)
            if last:
                await edit_or_send(last, force=True)
        except Exception as exc:  # pragma: no cover - platform specific
            logger.warning(f"Telegram progress delivery failed: {exc}")

    async def initialize(self) -> None:
        """Called when the plugin is activated."""
        if self._initialized:
            return

        archive_path = Path(get_astrbot_data_path(), "archive.txt")
        archive_path.parent.mkdir(parents=True, exist_ok=True)

        self._initialized = True
        logger.info("Media Downloader plugin initialized successfully")

    async def terminate(self) -> None:
        """Called when the plugin is disabled or reloaded."""
        logger.info("Media Downloader plugin terminated")

    def _get_section(self, section_name: str) -> dict[str, Any]:
        """Return a grouped config section."""
        section = self.config.get(section_name, {})
        return section if isinstance(section, dict) else {}

    def _get_common_config(self) -> dict[str, Any]:
        return self._get_section("common_config")

    def _get_video_config(self) -> dict[str, Any]:
        return self._get_section("video_config")

    def _get_image_config(self) -> dict[str, Any]:
        return self._get_section("image_config")

    def _get_rclone_config(self) -> dict[str, Any]:
        return self._get_section("rclone_config")

    def _get_plugin_upload_path(self, config_key: str) -> str:
        """Resolve uploaded plugin file path from config."""
        config_value = self._get_video_config().get(config_key)
        if config_value is None:
            config_value = self._get_image_config().get(config_key, [])

        rel_path = ""
        if isinstance(config_value, list) and config_value:
            rel_path = str(config_value[0])
        elif isinstance(config_value, str):
            rel_path = config_value

        if rel_path:
            return str(
                Path(
                    get_astrbot_plugin_data_path(),
                    "astrbot_plugin_mediadownloader",
                    rel_path,
                )
            )
        return ""

    def _get_download_folders(self) -> list[str]:
        """Get download folders based on rclone setting."""
        if self._get_rclone_config().get("rclone_upload", False):
            return self._get_rclone_config().get("rclone_folders", [])
        return self._get_video_config().get("download_folders", [])

    def _is_url(self, text: str) -> bool:
        return bool(re.match(r"^https?://", text))

    def _is_telegram_file_url(self, url: str) -> bool:
        return "/file/bot" in url

    def _collect_file_sources(self, event: AstrMessageEvent) -> tuple[list[str], str]:
        file_urls: list[str] = []
        filename_hint = ""

        for component in event.get_messages():
            if isinstance(component, File):
                if component.name and not filename_hint:
                    filename_hint = component.name
                if component.url:
                    file_urls.append(component.url)
                elif component.file:
                    file_urls.append(component.file)
            elif isinstance(component, Video):
                candidate = getattr(component, "file", "")
                if candidate:
                    file_urls.append(candidate)
                path_hint = getattr(component, "path", "")
                if path_hint and not filename_hint:
                    filename_hint = Path(path_hint).name
            elif isinstance(component, Record):
                candidate = getattr(component, "file", "")
                if candidate:
                    file_urls.append(candidate)

        normalized: list[str] = []
        for url in file_urls:
            if url.startswith("file://"):
                normalized.append(url[7:])
            else:
                normalized.append(url)

        return normalized, filename_hint

    def _build_selection_message(self, session_id: str, selected_idx: int = 0) -> str:
        """Build the folder selection message."""
        folders = self._get_download_folders()

        lines = ["请选择目录：\n"]
        for idx, folder in enumerate(folders, start=1):
            marker = "✅ " if idx - 1 == selected_idx else ""
            lines.append(f"{marker}{idx}. {folder}")

        state = SESSION_STATE.get(session_id, {})
        enable_archive = state.get(
            "enable_archive", self._get_common_config().get("enable_archive", True)
        )
        use_proxy = state.get(
            "use_proxy", self._get_video_config().get("video_proxy", False)
        )
        separate_folder = state.get(
            "video_separate_folder",
            self._get_video_config().get("video_seperate_folder", False),
        )
        default_action = state.get("default_action", "video")

        lines.append("\n配置选项：")
        lines.append(f"- 存档: {'开' if enable_archive else '关'}")
        lines.append(f"- 代理: {'开' if use_proxy else '关'}")
        lines.append(f"- 独立文件夹: {'开' if separate_folder else '关'}")
        lines.append(f"- 默认下载: {'音频' if default_action == 'audio' else '视频'}")

        lines.append("\n回复说明：")
        lines.append(f"- 数字(1-{len(folders)}): 选择目录")
        lines.append("- 存档: 切换存档选项")
        lines.append("- 代理: 切换代理选项")
        lines.append("- 独立文件夹: 切换目录结构")
        lines.append("- 视频: 开始下载视频")
        lines.append("- 音频: 仅下载音频")
        lines.append("- 开始: 使用默认模式下载")
        lines.append("- 取消: 退出当前会话")

        return "\n".join(lines)

    def _send_selection_keyboard(
        self, event: AstrMessageEvent, session_id: str, selected_idx: int | None = None
    ) -> MessageEventResult:
        """Build and return inline keyboard for Telegram platform."""
        folders = self._get_download_folders()
        state = SESSION_STATE.get(session_id, {})
        keyboard_session_id = state.get("keyboard_session_id", uuid.uuid4().hex[:8])

        if selected_idx is None:
            selected_idx = state.get("selected_folder_idx", 0)

        enable_archive = state.get(
            "enable_archive", self._get_common_config().get("enable_archive", True)
        )
        use_proxy = state.get(
            "use_proxy", self._get_video_config().get("video_proxy", False)
        )
        separate_folder = state.get(
            "video_separate_folder",
            self._get_video_config().get("video_seperate_folder", False),
        )

        keyboard = []

        # Folder selection buttons - one folder per row
        for idx, folder in enumerate(folders):
            marker = "✅ " if idx == selected_idx else ""
            keyboard.append(
                [
                    {
                        "text": f"{marker}{folder}",
                        "callback_data": f"vd:{keyboard_session_id}:folder:{idx}",
                    }
                ]
            )

        # Config toggle row
        keyboard.append(
            [
                {
                    "text": f"存档 {'✅' if enable_archive else '⭕'}",
                    "callback_data": f"vd:{keyboard_session_id}:toggle:archive",
                },
                {
                    "text": f"代理 {'✅' if use_proxy else '⭕'}",
                    "callback_data": f"vd:{keyboard_session_id}:toggle:proxy",
                },
                {
                    "text": f"独立 {'✅' if separate_folder else '⭕'}",
                    "callback_data": f"vd:{keyboard_session_id}:toggle:separate",
                },
            ]
        )

        # Action buttons row
        keyboard.append(
            [
                {
                    "text": "🎬 视频",
                    "callback_data": f"vd:{keyboard_session_id}:action:video",
                },
                {
                    "text": "🎵 音频",
                    "callback_data": f"vd:{keyboard_session_id}:action:audio",
                },
                {
                    "text": "❌ 取消",
                    "callback_data": f"vd:{keyboard_session_id}:action:cancel",
                },
            ]
        )

        current_folder = (
            folders[selected_idx] if 0 <= selected_idx < len(folders) else "-"
        )
        default_action = state.get("default_action", "video")

        result = MessageEventResult()
        result.message(
            "\n".join(
                [
                    "请选择下载目录和配置：",
                    f"当前目录：{current_folder}",
                    (
                        "选项："
                        f"存档 {'开' if enable_archive else '关'} | "
                        f"代理 {'开' if use_proxy else '关'} | "
                        f"独立文件夹 {'开' if separate_folder else '关'} | "
                        f"默认 {'音频' if default_action == 'audio' else '视频'}"
                    ),
                ]
            )
        )
        result.inline_keyboard(keyboard)
        return result

    def _init_session_state(
        self, session_id: str, default_action: str
    ) -> dict[str, Any]:
        state = {
            "stage": "select",
            "mode": "yt-dlp",
            "url": "",
            "file_urls": [],
            "filename_hint": "",
            "selected_folder_idx": 0,
            "enable_archive": self._get_common_config().get("enable_archive", True),
            "use_proxy": self._get_video_config().get("video_proxy", False),
            "video_separate_folder": self._get_video_config().get(
                "video_seperate_folder", False
            ),
            "default_action": default_action,
            "keyboard_session_id": uuid.uuid4().hex[:8],
        }
        SESSION_STATE[session_id] = state
        return state

    async def _start_command(self, event: AstrMessageEvent, default_action: str):
        await self.initialize()

        message = event.message_str.strip()
        command = "audio" if default_action == "audio" else "video"
        args_text = message.replace(command, "", 1).strip()

        session_id = str(event.unified_msg_origin)
        state = self._init_session_state(session_id, default_action)

        file_urls, filename_hint = self._collect_file_sources(event)
        if filename_hint:
            state["filename_hint"] = filename_hint

        if args_text and self._is_url(args_text):
            if self._is_telegram_file_url(args_text):
                state.update({"mode": "file", "file_urls": [args_text]})
            else:
                state.update({"mode": "yt-dlp", "url": args_text})
        elif args_text:
            state["filename_hint"] = args_text
            if file_urls:
                state.update({"mode": "file", "file_urls": file_urls})
            else:
                state["stage"] = "await_file"
        elif file_urls:
            state.update({"mode": "file", "file_urls": file_urls})
        else:
            usage = (
                "用法：/video <链接> 或 /video <文件名>\n"
                "支持 YouTube, Bilibili, Twitter 等平台的视频下载。\n"
                "在 Telegram 中可直接发送文件或文件链接。"
            )
            yield event.plain_result(usage)
            return

        if state["stage"] == "await_file":
            yield event.plain_result(
                "📤 请上传文件或发送下载链接。\n"
                f"保存文件名：{state['filename_hint']}\n"
                "回复 '取消' 退出。"
            )
        else:
            # Check if platform is Telegram - use inline keyboard
            if event.get_platform_name() == "telegram":
                result = self._send_selection_keyboard(event, session_id)
                event.set_result(result)
                return

            msg = self._build_selection_message(session_id)
            yield event.plain_result(msg)

        @session_waiter(timeout=SESSION_TIMEOUT)
        async def wait_for_reply(
            controller: SessionController, reply_event: AstrMessageEvent
        ) -> None:
            nonlocal session_id
            current_state = SESSION_STATE.get(session_id)
            if not current_state:
                controller.stop()
                return

            reply_text = reply_event.message_str.strip()

            if reply_text.lower() in ("取消", "cancel", "退出", "exit"):
                await reply_event.send(reply_event.plain_result("已取消操作。"))
                SESSION_STATE.pop(session_id, None)
                controller.stop()
                return

            if current_state["stage"] == "await_file":
                file_urls, filename_hint = self._collect_file_sources(reply_event)
                if filename_hint and not current_state.get("filename_hint"):
                    current_state["filename_hint"] = filename_hint

                if reply_text and self._is_url(reply_text):
                    if self._is_telegram_file_url(reply_text):
                        current_state.update(
                            {"mode": "file", "file_urls": [reply_text]}
                        )
                    else:
                        current_state.update({"mode": "yt-dlp", "url": reply_text})
                elif file_urls:
                    current_state.update({"mode": "file", "file_urls": file_urls})
                else:
                    await reply_event.send(
                        reply_event.plain_result(
                            "请发送文件或有效的下载链接，或回复 '取消' 退出。"
                        )
                    )
                    return

                current_state["stage"] = "select"
                # Check if platform is Telegram - use inline keyboard
                if reply_event.get_platform_name() == "telegram":
                    current_state["keyboard_pending"] = True
                    result = self._send_selection_keyboard(
                        reply_event,
                        session_id,
                        current_state.get("selected_folder_idx", 0),
                    )
                    reply_event.set_result(result)
                    controller.stop()
                else:
                    msg = self._build_selection_message(
                        session_id, current_state.get("selected_folder_idx", 0)
                    )
                    await reply_event.send(reply_event.plain_result(msg))
                return

            folders = self._get_download_folders()

            if reply_text.isdigit():
                idx = int(reply_text) - 1
                if 0 <= idx < len(folders):
                    current_state["selected_folder_idx"] = idx
                    msg = self._build_selection_message(session_id, idx)
                    await reply_event.send(reply_event.plain_result(msg))
                else:
                    await reply_event.send(
                        reply_event.plain_result(
                            f"❌ 无效的目录序号，请选择 1-{len(folders)}"
                        )
                    )
                return

            if reply_text in ("存档",):
                current_state["enable_archive"] = not current_state.get(
                    "enable_archive", True
                )
                msg = self._build_selection_message(
                    session_id, current_state.get("selected_folder_idx", 0)
                )
                await reply_event.send(reply_event.plain_result(msg))
                return

            if reply_text in ("代理",):
                current_state["use_proxy"] = not current_state.get("use_proxy", False)
                msg = self._build_selection_message(
                    session_id, current_state.get("selected_folder_idx", 0)
                )
                await reply_event.send(reply_event.plain_result(msg))
                return

            if reply_text in ("独立", "独立文件夹"):
                current_state["video_separate_folder"] = not current_state.get(
                    "video_separate_folder", False
                )
                msg = self._build_selection_message(
                    session_id, current_state.get("selected_folder_idx", 0)
                )
                await reply_event.send(reply_event.plain_result(msg))
                return

            if reply_text in ("视频", "音频", "开始", "下载"):
                action = reply_text
                if action in ("开始", "下载"):
                    action = (
                        "音频"
                        if current_state.get("default_action") == "audio"
                        else "视频"
                    )
                audio_only = action == "音频"

                controller.stop()
                if current_state.get("mode") == "file":
                    await self._handle_file_download(reply_event, current_state)
                else:
                    url = current_state.get("url", "")
                    if not url:
                        await reply_event.send(
                            reply_event.plain_result("❌ 未找到下载链接")
                        )
                        return
                    await self._handle_download(
                        reply_event, url, current_state, audio_only
                    )
                return

            await reply_event.send(
                reply_event.plain_result("⚠️ 无效输入，请按提示回复或回复 '取消' 退出")
            )

        try:
            await wait_for_reply(event)
        except TimeoutError:
            yield event.plain_result("⏰ 等待超时，操作已取消。")
        finally:
            state = SESSION_STATE.get(session_id)
            if not state or not state.get("keyboard_pending"):
                SESSION_STATE.pop(session_id, None)
            event.stop_event()

    @filter.command("video")
    async def video_command(self, event: AstrMessageEvent):
        """Download video or audio from URL or Telegram file."""
        async for result in self._start_command(event, "video"):
            yield result

    @filter.command("audio")
    async def audio_command(self, event: AstrMessageEvent):
        """Download audio from URL or Telegram file."""
        async for result in self._start_command(event, "audio"):
            yield result

    @filter.callback_query()
    async def handle_callback(self, event: TelegramCallbackQueryEvent) -> None:
        """Handle inline keyboard button callbacks for video downloader."""
        if not event.data.startswith("vd:"):
            return

        parts = event.data.split(":")
        if len(parts) < 4:
            return

        keyboard_session_id = parts[1]
        action_type = parts[2]
        action_value = parts[3]

        # Find session by keyboard_session_id
        session_id: str | None = None
        state: dict[str, Any] | None = None
        for sid, s in SESSION_STATE.items():
            if s.get("keyboard_session_id") == keyboard_session_id:
                session_id = sid
                state = s
                break

        if not state or not session_id:
            await event.answer_callback_query(text="会话已过期，请重新开始")
            return

        folders = self._get_download_folders()

        if action_type == "folder":
            idx = int(action_value)
            if 0 <= idx < len(folders):
                state["selected_folder_idx"] = idx
                result = self._send_selection_keyboard(event, session_id, idx)
                event.set_result(result)
                await event.answer_callback_query(text=f"已选择: {folders[idx]}")
            else:
                await event.answer_callback_query(text="无效的目录选择")

        elif action_type == "toggle":
            if action_value == "archive":
                state["enable_archive"] = not state.get("enable_archive", True)
                await event.answer_callback_query(
                    text=f"存档: {'开' if state['enable_archive'] else '关'}"
                )
            elif action_value == "proxy":
                state["use_proxy"] = not state.get("use_proxy", False)
                await event.answer_callback_query(
                    text=f"代理: {'开' if state['use_proxy'] else '关'}"
                )
            elif action_value == "separate":
                state["video_separate_folder"] = not state.get(
                    "video_separate_folder", False
                )
                await event.answer_callback_query(
                    text=f"独立文件夹: {'开' if state['video_separate_folder'] else '关'}"
                )
            # Refresh keyboard
            result = self._send_selection_keyboard(
                event, session_id, state.get("selected_folder_idx", 0)
            )
            event.set_result(result)

        elif action_type == "action":
            if action_value == "cancel":
                SESSION_STATE.pop(session_id, None)
                await event.answer_callback_query(text="已取消操作")
                result = MessageEventResult()
                result.message("❌ 已取消操作")
                event.set_result(result)
            else:
                await event.answer_callback_query(text="任务已开始")
                state["keyboard_pending"] = False
                audio_only = action_value == "audio"
                if state.get("mode") == "file":
                    await self._handle_file_download(event, state)
                else:
                    url = state.get("url", "")
                    if not url:
                        await event.answer_callback_query(text="未找到下载链接")
                        return
                    await self._handle_download(event, url, state, audio_only)
                SESSION_STATE.pop(session_id, None)

    async def _handle_download(
        self,
        event: AstrMessageEvent,
        url: str,
        state: dict[str, Any],
        audio_only: bool,
    ) -> None:
        """Handle the actual download process."""
        folders = self._get_download_folders()
        selected_idx = state.get("selected_folder_idx", 0)
        enable_archive = state.get("enable_archive", True)
        use_proxy = state.get("use_proxy", False)
        separate_folder = state.get("video_separate_folder", False)

        if not folders:
            await event.send(event.plain_result("❌ 未配置下载目录"))
            return

        select_path = folders[selected_idx]

        if self._get_rclone_config().get("rclone_upload", False):
            download_folder = Path(get_astrbot_temp_path(), "video_downloader")
        else:
            download_folder = Path(select_path)
        download_folder.mkdir(parents=True, exist_ok=True)

        if separate_folder:
            outtmpl = str(
                download_folder
                / "%(uploader)s"
                / "%(title).20s"
                / "%(title).100s.%(ext)s"
            )
        else:
            outtmpl = str(
                download_folder
                / "%(uploader)s"
                / "%(uploader)s投稿"
                / "%(title).100s.%(ext)s"
            )

        # Get cookie file path
        cookie_file = self._get_plugin_upload_path("cookie_file")
        proxy_url = (
            self._get_video_config().get("video_proxy_url", "") if use_proxy else ""
        )
        archive_path = str(Path(get_astrbot_data_path(), "archive.txt"))

        downloaded_files: list[str] = []
        last_error: str | None = None

        async def download_stream() -> AsyncGenerator[str, None]:
            nonlocal downloaded_files, last_error
            yield "⏳ 开始下载..."
            attempt = 0
            while attempt < MAX_RETRIES:
                downloaded_files = []
                failed = False
                async for state_type, data in download_with_yt_dlp(
                    url,
                    outtmpl,
                    cookie_file,
                    proxy_url,
                    audio_only,
                    enable_archive,
                    archive_path,
                ):
                    if state_type == "progress":
                        yield f"📥 下载中：{data}"
                    elif state_type == "save_path":
                        downloaded_files.append(data)
                    elif state_type == "failed":
                        failed = True
                        last_error = data
                        yield f"❌ 下载失败：{data}，重试 {attempt + 1}/{MAX_RETRIES}"
                        break
                    elif state_type == "success" and not downloaded_files:
                        yield f"✅ 下载完成：{data}"
                        break

                if failed:
                    attempt += 1
                    if attempt >= MAX_RETRIES:
                        yield "❌ 下载失败，已达到最大重试次数"
                        return
                    await asyncio.sleep(5)
                    yield f"⏳ 正在重试（{attempt}/{MAX_RETRIES}）..."
                    continue

                break

        await self._send_stream_updates(event, download_stream)

        if downloaded_files:
            await self._process_downloaded_files(
                event, downloaded_files, download_folder, select_path
            )
        elif self._get_rclone_config().get("rclone_upload", False) and not last_error:
            await self._handle_rclone_directory_transfer(
                event, download_folder, select_path
            )

    async def _handle_file_download(
        self, event: AstrMessageEvent, state: dict[str, Any]
    ) -> None:
        file_urls = state.get("file_urls", [])
        if not file_urls:
            await event.send(event.plain_result("❌ 未找到可下载的文件"))
            return

        folders = self._get_download_folders()
        if not folders:
            await event.send(event.plain_result("❌ 未配置下载目录"))
            return

        selected_idx = state.get("selected_folder_idx", 0)
        select_path = folders[selected_idx]

        if self._get_rclone_config().get("rclone_upload", False):
            download_folder = Path(get_astrbot_temp_path(), "video_downloader")
        else:
            download_folder = Path(select_path)
        download_folder.mkdir(parents=True, exist_ok=True)

        filename_hint = state.get("filename_hint", "")
        downloaded_files: list[str] = []

        for idx, file_url in enumerate(file_urls, start=1):
            filename = determine_filename(filename_hint, [file_url])
            if len(file_urls) > 1:
                stem = Path(filename).stem
                suffix = Path(filename).suffix
                filename = f"{stem}_{idx}{suffix}"

            save_path = download_folder / filename
            await event.send(
                event.plain_result(
                    f"📥 正在下载文件 {idx}/{len(file_urls)}: {filename}"
                )
            )

            success, _ = await download_file(file_url, save_path)
            if success:
                downloaded_files.append(str(save_path))
            else:
                await event.send(event.plain_result(f"❌ 文件下载失败: {filename}"))

        if downloaded_files:
            await self._process_downloaded_files(
                event, downloaded_files, download_folder, select_path
            )
        else:
            await event.send(event.plain_result("❌ 所有文件下载失败"))

    async def _process_downloaded_files(
        self,
        event: AstrMessageEvent,
        downloaded_files: list[str],
        download_folder: Path,
        select_path: str,
    ) -> None:
        """Process downloaded files and optionally transfer via rclone."""
        results = []

        for local_path in downloaded_files:
            local_path_obj = Path(local_path).resolve()

            if self._get_rclone_config().get("rclone_upload", False):
                try:
                    rel_path = local_path_obj.relative_to(download_folder)
                except ValueError:
                    rel_path = Path(os.path.relpath(local_path_obj, download_folder))
                remote_dir = rel_path.parent
                remote_name = self._get_rclone_config().get("rclone_server", "")
                remote_path = (
                    str(Path(select_path) / remote_dir)
                    if str(remote_dir) not in ("", ".")
                    else select_path
                )

                async def transfer_stream() -> AsyncGenerator[str, None]:
                    yield f"✅ 下载完成：{local_path_obj.name}，正在传输..."
                    async for state_type, data in rclone_transfer(
                        local_path_obj, remote_name, remote_path
                    ):
                        if state_type == "progress":
                            yield f"📤 传输中：{data}"
                        elif state_type == "success":
                            results.append(f"✅ {local_path_obj.name}")
                            yield f"✅ 传输完成：{local_path_obj.name}"
                            return
                        elif state_type == "failed":
                            results.append(f"❌ {local_path_obj.name}: {data}")
                            yield f"❌ 传输失败：{local_path_obj.name}: {data}"
                            return

                await self._send_stream_updates(event, transfer_stream)
            else:
                results.append(f"✅ {local_path_obj}")

        await event.send(
            event.plain_result("\n".join(results) if results else "下载完成")
        )

    async def _handle_rclone_directory_transfer(
        self,
        event: AstrMessageEvent,
        download_folder: Path,
        select_path: str,
    ) -> None:
        """Handle rclone transfer of entire directory."""
        remote_name = self._get_rclone_config().get("rclone_server", "")

        async def transfer_stream() -> AsyncGenerator[str, None]:
            yield "📤 正在传输文件夹..."
            async for state_type, data in rclone_move_directory(
                download_folder, remote_name, select_path
            ):
                if state_type == "progress":
                    yield f"📤 传输中：{data}"
                elif state_type == "success":
                    yield f"✅ 传输完成：{data}"
                    return
                elif state_type == "failed":
                    yield f"❌ 传输失败：{data}"
                    return

        await self._send_stream_updates(event, transfer_stream)

    @filter.command("image")
    async def image_command(self, event: AstrMessageEvent):
        """Download images using gallery-dl or ktoolbox."""
        await self.initialize()

        url = event.message_str.replace("image", "", 1).strip()
        if not url or not self._is_url(url):
            yield event.plain_result(
                "用法：/image <链接>\n"
                "Kemono 链接会使用 ktoolbox，其他受支持图片站点会使用 gallery-dl。"
            )
            return

        await self._handle_image_download(event, url)

    async def _handle_image_download(
        self,
        event: AstrMessageEvent,
        url: str,
    ) -> None:
        """Handle gallery-dl or ktoolbox image downloads."""
        use_rclone = self._get_rclone_config().get("rclone_upload", False)
        target_path = (
            self._get_rclone_config().get("image_rclone_folder", "")
            if use_rclone
            else self._get_image_config().get("image_download_folder", "")
        )

        if not target_path:
            message = "❌ 未配置图片远端目录" if use_rclone else "❌ 未配置图片下载目录"
            await event.send(event.plain_result(message))
            return

        temp_root = Path(
            get_astrbot_temp_path(), "video_downloader_images", uuid.uuid4().hex
        )
        download_root = temp_root if use_rclone else Path(target_path)
        download_root.mkdir(parents=True, exist_ok=True)
        existing_files = {
            path.relative_to(download_root)
            for path in download_root.rglob("*")
            if path.is_file()
        }

        if is_ktoolbox_url(url):
            tool_name = "ktoolbox"
            config_file = self._get_plugin_upload_path("ktoolbox_config_file")
            cookie_file = self._get_plugin_upload_path("ktoolbox_cookie_file")
            session_key = extract_session_key_from_cookie_file(cookie_file)
            workspace = Path(
                get_astrbot_temp_path(),
                "video_downloader_ktoolbox",
                uuid.uuid4().hex,
            )
            prepare_ktoolbox_env(workspace, config_file, session_key)

            def stream_factory() -> AsyncGenerator[str, None]:
                return self._stream_ktoolbox_download(url, workspace, download_root)

        else:
            tool_name = "gallery-dl"
            config_file = self._get_plugin_upload_path("gallery_dl_config_file")
            cookie_file = self._get_plugin_upload_path("gallery_dl_cookie_file")

            def stream_factory() -> AsyncGenerator[str, None]:
                return self._stream_gallery_dl_download(
                    url, download_root, config_file, cookie_file
                )

        await event.send(event.plain_result(f"🖼️ 使用 {tool_name} 开始下载..."))
        await self._send_stream_updates(event, stream_factory)

        new_files = {
            path.relative_to(download_root)
            for path in download_root.rglob("*")
            if path.is_file()
        }
        if not new_files.difference(existing_files):
            await event.send(event.plain_result("❌ 未检测到已下载文件"))
            return

        if use_rclone:
            await self._handle_rclone_directory_transfer(
                event, download_root, target_path
            )
        else:
            await event.send(event.plain_result(f"✅ 图片下载完成：{download_root}"))

    async def _stream_gallery_dl_download(
        self,
        url: str,
        download_root: Path,
        config_file: str,
        cookie_file: str,
    ) -> AsyncGenerator[str, None]:
        archive_path = str(Path(get_astrbot_data_path(), "archive-gallery.txt"))
        yield "⏳ gallery-dl 开始下载..."
        async for state_type, data in download_with_gallery_dl(
            url,
            download_root,
            config_file=config_file,
            cookie_file=cookie_file,
            enable_archive=self._get_common_config().get("enable_archive", True),
            archive_path=archive_path,
        ):
            if state_type == "progress":
                yield f"📥 {data}"
            elif state_type == "failed":
                yield f"❌ gallery-dl 下载失败：{data}"
                return
            elif state_type == "success":
                yield "✅ gallery-dl 下载完成"
                return

    async def _stream_ktoolbox_download(
        self,
        url: str,
        workspace: Path,
        download_root: Path,
    ) -> AsyncGenerator[str, None]:
        yield "⏳ ktoolbox 开始下载..."
        async for state_type, data in download_with_ktoolbox(
            url, workspace, download_root
        ):
            if state_type == "progress":
                yield f"📥 {data}"
            elif state_type == "failed":
                yield f"❌ ktoolbox 下载失败：{data}"
                return
            elif state_type == "success":
                yield "✅ ktoolbox 下载完成"
                return
