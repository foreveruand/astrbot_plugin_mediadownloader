"""
AstrBot Media Downloader Plugin.

This plugin provides video, audio, and image download functionality
with optional rclone upload support.
"""

import asyncio
import os
import re
import uuid
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.message_components import File, Record, Video
from astrbot.api.util import SessionController, SessionWaiter, session_waiter
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

    def _get_clash_config(self) -> tuple[str, str, list[str]]:
        """Return the configured Clash controller, group, and allowed nodes.

        Returns:
            Tuple containing controller URL, proxy group name, and allowed node names.
        """
        video_config = self._get_video_config()
        raw_nodes = video_config.get("clash_nodes", [])
        nodes = (
            [str(node).strip() for node in raw_nodes if str(node).strip()]
            if isinstance(raw_nodes, list)
            else []
        )
        return (
            str(video_config.get("clash_controller_url", "")).rstrip("/"),
            str(video_config.get("clash_proxy_group", "")).strip(),
            nodes,
        )

    def _has_clash_config(self) -> bool:
        """Return whether Clash node switching is fully configured.

        Returns:
            Whether the controller URL, proxy group, and allowed nodes are configured.
        """
        controller_url, group_name, nodes = self._get_clash_config()
        return bool(controller_url and group_name and nodes)

    async def _refresh_clash_menu_state(self, state: dict[str, Any]) -> None:
        """Fetch the current Clash node and initialize a menu selection when possible.

        Args:
            state: Ephemeral interactive download state.
        """
        if not self._has_clash_config():
            return

        current_node, error = await self._get_current_clash_node()
        state["clash_current_node"] = current_node
        state["clash_error"] = error
        _, _, nodes = self._get_clash_config()
        if "clash_selected_node" not in state and current_node in nodes:
            state["clash_selected_node"] = current_node

    async def _get_current_clash_node(self) -> tuple[str, str]:
        """Read the active node of the configured Clash proxy group.

        Returns:
            Tuple containing the active node name and an error message when unavailable.
        """
        controller_url, group_name, _ = self._get_clash_config()
        endpoint = f"{controller_url}/proxies/{quote(group_name, safe='')}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(endpoint)
                response.raise_for_status()
            current_node = response.json().get("now")
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(f"Unable to read Clash proxy group {group_name}: {exc}")
            return "", f"无法读取 Clash 当前节点：{exc}"

        if not isinstance(current_node, str) or not current_node:
            return "", "Clash 策略组未返回当前节点"
        return current_node, ""

    async def _switch_clash_node(self, node_name: str) -> str:
        """Switch the configured Clash proxy group to a node.

        Args:
            node_name: Node name to select for the configured proxy group.

        Returns:
            Empty text on success, otherwise a user-facing error message.
        """
        controller_url, group_name, _ = self._get_clash_config()
        endpoint = f"{controller_url}/proxies/{quote(group_name, safe='')}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.put(endpoint, json={"name": node_name})
                response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                f"Unable to switch Clash proxy group {group_name} to {node_name}: {exc}"
            )
            return f"无法切换 Clash 节点：{exc}"
        return ""

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
        clash_enabled = self._has_clash_config()
        clash_current_node = state.get("clash_current_node", "")
        clash_selected_node = state.get("clash_selected_node", "")

        lines.append("\n配置选项：")
        lines.append(f"- 存档: {'开' if enable_archive else '关'}")
        lines.append(f"- 代理: {'开' if use_proxy else '关'}")
        lines.append(f"- 独立文件夹: {'开' if separate_folder else '关'}")
        lines.append(f"- 默认下载: {'音频' if default_action == 'audio' else '视频'}")
        if clash_enabled:
            lines.append(f"- Clash 当前节点: {clash_current_node or '读取失败'}")
            lines.append(f"- Clash 下载节点: {clash_selected_node or '请选择'}")

        lines.append("\n回复说明：")
        lines.append(f"- 数字(1-{len(folders)}): 选择目录")
        lines.append("- 存档: 切换存档选项")
        lines.append("- 代理: 切换代理选项")
        lines.append("- 独立文件夹: 切换目录结构")
        lines.append("- 视频: 开始下载视频")
        lines.append("- 音频: 仅下载音频")
        lines.append("- 开始: 使用默认模式下载")
        if clash_enabled:
            lines.append("- Clash节点 <序号>: 选择本次下载使用的 Clash 节点")
        lines.append("- 取消: 退出当前会话")

        return "\n".join(lines)

    def _build_selection_keyboard_content(
        self, session_id: str, selected_idx: int | None = None
    ) -> tuple[str, list[list[dict[str, str]]]]:
        """Build Telegram selection menu text and inline keyboard.

        Args:
            session_id: Interactive download session ID.
            selected_idx: Optional selected folder index override.

        Returns:
            Menu text and AstrBot inline keyboard data.
        """
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
        clash_enabled = self._has_clash_config()
        clash_current_node = state.get("clash_current_node", "")
        clash_selected_node = state.get("clash_selected_node", "")

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

        if clash_enabled:
            _, _, clash_nodes = self._get_clash_config()
            keyboard.append(
                [
                    {
                        "text": (
                            f"{'✅ ' if node_name == clash_selected_node else ''}"
                            f"Clash {node_name}"
                        ),
                        "callback_data": f"vd:{keyboard_session_id}:clash:{idx}",
                    }
                    for idx, node_name in enumerate(clash_nodes)
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

        text = "\n".join(
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
                *(
                    [
                        f"Clash 当前：{clash_current_node or '读取失败'}",
                        f"Clash 下载节点：{clash_selected_node or '请选择'}",
                    ]
                    if clash_enabled
                    else []
                ),
            ]
        )
        return text, keyboard

    def _send_selection_keyboard(
        self, event: AstrMessageEvent, session_id: str, selected_idx: int | None = None
    ) -> MessageEventResult:
        """Build and return inline keyboard for Telegram platform."""
        text, keyboard = self._build_selection_keyboard_content(
            session_id, selected_idx
        )
        result = MessageEventResult()
        result.message(text)
        result.inline_keyboard(keyboard)
        return result

    def _get_telegram_chat_payload(self, event: AstrMessageEvent) -> dict[str, Any]:
        """Build Telegram chat payload for the event session.

        Args:
            event: AstrBot message event from Telegram.

        Returns:
            Telegram API chat payload with optional thread ID.
        """
        chat_id = event.get_sender_id()
        message_thread_id = None
        if event.get_message_type().name == "GROUP_MESSAGE":
            chat_id = getattr(event.message_obj, "group_id", chat_id)
        if "#" in str(chat_id):
            chat_id, message_thread_id = str(chat_id).split("#", 1)

        payload: dict[str, Any] = {"chat_id": chat_id}
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id
        return payload

    def _build_telegram_reply_markup(
        self, keyboard: list[list[dict[str, str]]]
    ) -> InlineKeyboardMarkup:
        """Convert AstrBot inline keyboard data to Telegram markup.

        Args:
            keyboard: AstrBot inline keyboard data.

        Returns:
            Telegram inline keyboard markup.
        """
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(**button) for button in row] for row in keyboard]
        )

    async def _send_telegram_selection_menu(
        self, event: AstrMessageEvent, session_id: str, selected_idx: int | None = None
    ) -> bool:
        """Send Telegram selection menu directly and store message identity.

        Args:
            event: Telegram message event used for chat routing.
            session_id: Interactive download session ID.
            selected_idx: Optional selected folder index override.

        Returns:
            Whether the Telegram menu was sent and tracked.
        """
        client = getattr(event, "client", None)
        if client is None:
            return False

        state = SESSION_STATE.get(session_id)
        if state is not None:
            await self._refresh_clash_menu_state(state)
        text, keyboard = self._build_selection_keyboard_content(
            session_id, selected_idx
        )
        payload = self._get_telegram_chat_payload(event)
        try:
            message = await client.send_message(
                text=text,
                reply_markup=self._build_telegram_reply_markup(keyboard),
                **payload,
            )
        except Exception as exc:  # pragma: no cover - platform specific
            logger.warning(f"Telegram selection menu send failed: {exc}")
            return False

        state = SESSION_STATE.get(session_id)
        if state is not None:
            state["telegram_menu"] = {
                "chat_id": payload["chat_id"],
                "message_id": message.message_id,
            }
            if payload.get("message_thread_id"):
                state["telegram_menu"]["message_thread_id"] = payload[
                    "message_thread_id"
                ]
        return True

    async def _send_telegram_session_message(
        self, event: AstrMessageEvent, session_id: str, text: str
    ) -> bool:
        """Send a Telegram session message and store message identity.

        Args:
            event: Telegram message event used for chat routing.
            session_id: Interactive download session ID.
            text: Message text to send.

        Returns:
            Whether the Telegram message was sent and tracked.
        """
        client = getattr(event, "client", None)
        if client is None:
            return False

        payload = self._get_telegram_chat_payload(event)
        try:
            message = await client.send_message(text=text, **payload)
        except Exception as exc:  # pragma: no cover - platform specific
            logger.warning(f"Telegram session message send failed: {exc}")
            return False

        state = SESSION_STATE.get(session_id)
        if state is not None:
            state["telegram_menu"] = {
                "chat_id": payload["chat_id"],
                "message_id": message.message_id,
            }
            if payload.get("message_thread_id"):
                state["telegram_menu"]["message_thread_id"] = payload[
                    "message_thread_id"
                ]
        return True

    async def _delete_telegram_user_message(self, event: AstrMessageEvent) -> None:
        """Delete the Telegram user reply when possible.

        Args:
            event: Telegram message event to delete.
        """
        client = getattr(event, "client", None)
        message_id = getattr(event.message_obj, "message_id", None)
        if client is None or not message_id:
            return

        payload = self._get_telegram_chat_payload(event)
        try:
            await client.delete_message(
                chat_id=payload["chat_id"], message_id=int(message_id)
            )
        except Exception as exc:  # pragma: no cover - platform specific
            logger.debug(f"Telegram user message delete failed: {exc}")

    async def _refresh_telegram_selection_menu(
        self,
        event: AstrMessageEvent,
        session_id: str,
        selected_idx: int | None = None,
        status: str = "",
    ) -> bool:
        """Edit the stored Telegram selection menu or send a replacement.

        Args:
            event: Telegram message event used for fallback chat routing.
            session_id: Interactive download session ID.
            selected_idx: Optional selected folder index override.
            status: Optional status line prepended to the menu text.

        Returns:
            Whether the menu was edited or replaced.
        """
        client = getattr(event, "client", None)
        if client is None:
            return False

        state = SESSION_STATE.get(session_id, {})
        await self._refresh_clash_menu_state(state)
        text, keyboard = self._build_selection_keyboard_content(
            session_id, selected_idx
        )
        if status:
            text = f"{status}\n\n{text}"

        menu = state.get("telegram_menu")
        if menu:
            try:
                await client.edit_message_text(
                    text=text,
                    chat_id=menu["chat_id"],
                    message_id=menu["message_id"],
                    reply_markup=self._build_telegram_reply_markup(keyboard),
                )
                return True
            except Exception as exc:  # pragma: no cover - platform specific
                logger.warning(f"Telegram selection menu edit failed: {exc}")

        return await self._send_telegram_selection_menu(event, session_id, selected_idx)

    async def _finish_telegram_selection_menu(
        self, event: AstrMessageEvent, session_id: str, text: str
    ) -> bool:
        """Edit the stored Telegram selection menu to a terminal state.

        Args:
            event: Telegram message event kept for client access.
            session_id: Interactive download session ID.
            text: Final message text.

        Returns:
            Whether the tracked message was edited.
        """
        client = getattr(event, "client", None)
        menu = SESSION_STATE.get(session_id, {}).get("telegram_menu")
        if client is None or not menu:
            return False

        try:
            await client.edit_message_text(
                text=text,
                chat_id=menu["chat_id"],
                message_id=menu["message_id"],
                reply_markup=None,
            )
            return True
        except Exception as exc:  # pragma: no cover - platform specific
            logger.warning(f"Telegram selection menu finish failed: {exc}")
            return False

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
            await_file_message = (
                "📤 请上传文件或发送下载链接。\n"
                f"保存文件名：{state['filename_hint']}\n"
                "回复 '取消' 退出。"
            )
            if event.get_platform_name() == "telegram":
                if not await self._send_telegram_session_message(
                    event, session_id, await_file_message
                ):
                    yield event.plain_result(await_file_message)
            else:
                yield event.plain_result(await_file_message)
        else:
            # Check if platform is Telegram - use inline keyboard
            if event.get_platform_name() == "telegram":
                if not await self._send_telegram_selection_menu(event, session_id):
                    await self._refresh_clash_menu_state(state)
                    result = self._send_selection_keyboard(event, session_id)
                    event.set_result(result)

            else:
                await self._refresh_clash_menu_state(state)
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
            is_telegram = reply_event.get_platform_name() == "telegram"

            if is_telegram:
                reply_event.stop_event()
                await self._delete_telegram_user_message(reply_event)

            if reply_text.lower() in ("取消", "cancel", "退出", "exit"):
                if is_telegram:
                    if not await self._finish_telegram_selection_menu(
                        reply_event, session_id, "❌ 已取消操作"
                    ):
                        await reply_event.send(reply_event.plain_result("已取消操作。"))
                else:
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
                    if is_telegram:
                        if not await self._finish_telegram_selection_menu(
                            reply_event,
                            session_id,
                            "⚠️ 请发送文件或有效的下载链接，或回复 '取消' 退出。\n"
                            f"保存文件名：{current_state['filename_hint']}",
                        ):
                            await reply_event.send(
                                reply_event.plain_result(
                                    "请发送文件或有效的下载链接，或回复 '取消' 退出。"
                                )
                            )
                    else:
                        await reply_event.send(
                            reply_event.plain_result(
                                "请发送文件或有效的下载链接，或回复 '取消' 退出。"
                            )
                        )
                    return

                current_state["stage"] = "select"
                # Check if platform is Telegram - use inline keyboard
                if is_telegram:
                    await self._refresh_telegram_selection_menu(
                        reply_event,
                        session_id,
                        current_state.get("selected_folder_idx", 0),
                    )
                else:
                    await self._refresh_clash_menu_state(current_state)
                    msg = self._build_selection_message(
                        session_id, current_state.get("selected_folder_idx", 0)
                    )
                    await reply_event.send(reply_event.plain_result(msg))
                return

            folders = self._get_download_folders()

            clash_match = re.fullmatch(r"Clash节点\s+(\d+)", reply_text)
            if clash_match and self._has_clash_config():
                _, _, clash_nodes = self._get_clash_config()
                clash_idx = int(clash_match.group(1)) - 1
                if not 0 <= clash_idx < len(clash_nodes):
                    await reply_event.send(
                        reply_event.plain_result(
                            f"❌ 无效的 Clash 节点序号，请选择 1-{len(clash_nodes)}"
                        )
                    )
                    return
                current_state["clash_selected_node"] = clash_nodes[clash_idx]
                if is_telegram:
                    await self._refresh_telegram_selection_menu(
                        reply_event,
                        session_id,
                        current_state.get("selected_folder_idx", 0),
                    )
                else:
                    await self._refresh_clash_menu_state(current_state)
                    msg = self._build_selection_message(
                        session_id, current_state.get("selected_folder_idx", 0)
                    )
                    await reply_event.send(reply_event.plain_result(msg))
                return

            if reply_text.isdigit():
                idx = int(reply_text) - 1
                if 0 <= idx < len(folders):
                    current_state["selected_folder_idx"] = idx
                    if is_telegram:
                        await self._refresh_telegram_selection_menu(
                            reply_event, session_id, idx
                        )
                    else:
                        await self._refresh_clash_menu_state(current_state)
                        msg = self._build_selection_message(session_id, idx)
                        await reply_event.send(reply_event.plain_result(msg))
                else:
                    if is_telegram:
                        await self._refresh_telegram_selection_menu(
                            reply_event,
                            session_id,
                            current_state.get("selected_folder_idx", 0),
                            f"❌ 无效的目录序号，请选择 1-{len(folders)}",
                        )
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
                if is_telegram:
                    await self._refresh_telegram_selection_menu(
                        reply_event,
                        session_id,
                        current_state.get("selected_folder_idx", 0),
                    )
                else:
                    await self._refresh_clash_menu_state(current_state)
                    msg = self._build_selection_message(
                        session_id, current_state.get("selected_folder_idx", 0)
                    )
                    await reply_event.send(reply_event.plain_result(msg))
                return

            if reply_text in ("代理",):
                current_state["use_proxy"] = not current_state.get("use_proxy", False)
                if is_telegram:
                    await self._refresh_telegram_selection_menu(
                        reply_event,
                        session_id,
                        current_state.get("selected_folder_idx", 0),
                    )
                else:
                    await self._refresh_clash_menu_state(current_state)
                    msg = self._build_selection_message(
                        session_id, current_state.get("selected_folder_idx", 0)
                    )
                    await reply_event.send(reply_event.plain_result(msg))
                return

            if reply_text in ("独立", "独立文件夹"):
                current_state["video_separate_folder"] = not current_state.get(
                    "video_separate_folder", False
                )
                if is_telegram:
                    await self._refresh_telegram_selection_menu(
                        reply_event,
                        session_id,
                        current_state.get("selected_folder_idx", 0),
                    )
                else:
                    await self._refresh_clash_menu_state(current_state)
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
                current_state["keyboard_pending"] = False
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
                SESSION_STATE.pop(session_id, None)
                return

            if is_telegram:
                await self._refresh_telegram_selection_menu(
                    reply_event,
                    session_id,
                    current_state.get("selected_folder_idx", 0),
                    "⚠️ 无效输入，请按提示回复或回复 '取消' 退出",
                )
            else:
                await reply_event.send(
                    reply_event.plain_result(
                        "⚠️ 无效输入，请按提示回复或回复 '取消' 退出"
                    )
                )

        try:
            await wait_for_reply(event)
        except TimeoutError:
            if event.get_platform_name() == "telegram":
                if not await self._finish_telegram_selection_menu(
                    event, session_id, "⏰ 等待超时，操作已取消。"
                ):
                    yield event.plain_result("⏰ 等待超时，操作已取消。")
            else:
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

        # Callback events do not carry a message_id for result decoration. Route
        # them through the original command session, which already edits the menu.
        event.stop_event()

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
            try:
                idx = int(action_value)
            except ValueError:
                await event.answer_callback_query(text="无效的目录选择")
                return
            if not 0 <= idx < len(folders):
                await event.answer_callback_query(text="无效的目录选择")
                return
            event.message_str = str(idx + 1)
            await event.answer_callback_query(text=f"已选择: {folders[idx]}")

        elif action_type == "clash":
            _, _, clash_nodes = self._get_clash_config()
            try:
                clash_idx = int(action_value)
            except ValueError:
                await event.answer_callback_query(text="无效的 Clash 节点选择")
                return
            if not self._has_clash_config() or not 0 <= clash_idx < len(clash_nodes):
                await event.answer_callback_query(text="无效的 Clash 节点选择")
                return
            event.message_str = f"Clash节点 {clash_idx + 1}"
            await event.answer_callback_query(text=f"已选择: {clash_nodes[clash_idx]}")

        elif action_type == "toggle":
            toggle_text = {
                "archive": "存档",
                "proxy": "代理",
                "separate": "独立文件夹",
            }.get(action_value)
            if toggle_text is None:
                await event.answer_callback_query(text="无效的选项")
                return
            if action_value == "archive":
                enabled = not state.get("enable_archive", True)
                await event.answer_callback_query(
                    text=f"存档: {'开' if enabled else '关'}"
                )
            elif action_value == "proxy":
                enabled = not state.get("use_proxy", False)
                await event.answer_callback_query(
                    text=f"代理: {'开' if enabled else '关'}"
                )
            else:
                enabled = not state.get("video_separate_folder", False)
                await event.answer_callback_query(
                    text=f"独立文件夹: {'开' if enabled else '关'}"
                )
            event.message_str = toggle_text

        elif action_type == "action":
            action_text = {"video": "视频", "audio": "音频", "cancel": "取消"}.get(
                action_value
            )
            if action_text is None:
                await event.answer_callback_query(text="无效操作")
                return
            event.message_str = action_text
            await event.answer_callback_query(
                text="已取消操作" if action_value == "cancel" else "任务已开始"
            )
        else:
            await event.answer_callback_query(text="无效操作")
            return

        await SessionWaiter.trigger(session_id, event)

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
        cookie_browser = str(self._get_video_config().get("cookie_browser", "")).strip()
        proxy_url = (
            self._get_video_config().get("video_proxy_url", "") if use_proxy else ""
        )
        archive_path = str(Path(get_astrbot_data_path(), "archive.txt"))

        if not cookie_file and not cookie_browser:
            logger.info("No yt-dlp cookies configured for url=%s", url)
        elif cookie_browser:
            logger.info("Using browser cookies for yt-dlp: %s", cookie_browser)
        else:
            logger.info("Using uploaded yt-dlp cookie file: %s", cookie_file)

        clash_original_node = ""
        clash_selected_node = state.get("clash_selected_node", "")
        clash_switched = False
        if self._has_clash_config():
            _, _, clash_nodes = self._get_clash_config()
            if clash_selected_node not in clash_nodes:
                logger.warning(
                    "Download blocked before yt-dlp start: no valid Clash node selected"
                )
                await event.send(
                    event.plain_result("❌ 请先在下载菜单中选择一个 Clash 节点")
                )
                return

            clash_original_node, clash_error = await self._get_current_clash_node()
            if clash_error:
                await event.send(event.plain_result(f"❌ {clash_error}"))
                return

            if clash_selected_node != clash_original_node:
                clash_error = await self._switch_clash_node(clash_selected_node)
                if clash_error:
                    await event.send(event.plain_result(f"❌ {clash_error}"))
                    return
                clash_switched = True

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
                    cookie_browser=cookie_browser,
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
                        yield "✅ 下载完成" if not data else f"✅ 下载完成：{data}"
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

        try:
            if clash_switched:
                await event.send(
                    event.plain_result(f"🔀 Clash 已切换至：{clash_selected_node}")
                )
            await self._send_stream_updates(event, download_stream)
        finally:
            if clash_switched:
                restore_error = await self._switch_clash_node(clash_original_node)
                if restore_error:
                    await event.send(
                        event.plain_result(
                            f"⚠️ 下载结束，但未能恢复 Clash 节点：{restore_error}"
                        )
                    )
                else:
                    await event.send(
                        event.plain_result(f"↩️ Clash 已恢复至：{clash_original_node}")
                    )

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
                return self._stream_ktoolbox_download(
                    url, workspace, download_root, on_stop=event.is_stopped
                )

        else:
            tool_name = "gallery-dl"
            config_file = self._get_plugin_upload_path("gallery_dl_config_file")
            cookie_file = self._get_plugin_upload_path("gallery_dl_cookie_file")

            def stream_factory() -> AsyncGenerator[str, None]:
                return self._stream_gallery_dl_download(
                    url,
                    download_root,
                    config_file,
                    cookie_file,
                    on_stop=event.is_stopped,
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
        on_stop: Callable[[], bool] | None = None,
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
            on_stop=on_stop,
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
        on_stop: Callable[[], bool] | None = None,
    ) -> AsyncGenerator[str, None]:
        yield "⏳ ktoolbox 开始下载..."
        async for state_type, data in download_with_ktoolbox(
            url, workspace, download_root, on_stop=on_stop
        ):
            if state_type == "progress":
                yield f"📥 {data}"
            elif state_type == "failed":
                yield f"❌ ktoolbox 下载失败：{data}"
                return
            elif state_type == "success":
                yield "✅ ktoolbox 下载完成"
                return
