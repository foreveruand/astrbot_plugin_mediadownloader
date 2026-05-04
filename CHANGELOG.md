# Changelog

All notable changes to this project will be documented in this file.

## [1.2.4] - 2026-05-05

### Fixed
- Added `--js-runtimes node` to all `yt-dlp` invocations so YouTube extraction keeps working with the bundled Node.js runtime from `yt-dlp[node]`.

### Changed
- Simplified plugin dependencies to use `yt-dlp[node]` as the single `yt-dlp` requirement.

## [1.2.3] - 2026-05-05

### Changed
- Removed the Python `ktoolbox` package from plugin dependencies to avoid dependency conflicts.
- Kept Kemono support by invoking the external `ktoolbox` command instead.

### Fixed
- Added a clearer error when the external `ktoolbox` command is not installed or not available in `PATH`.

## [1.2.2] - 2026-05-05

### Changed
- Grouped plugin settings into second-level config sections for clearer navigation in the admin UI.
- Split settings into `common_config`, `video_config`, `image_config`, and `rclone_config`.

## [1.2.1] - 2026-05-05

### Changed
- Renamed the plugin from `astrbot_plugin_videodownloader` to `astrbot_plugin_mediadownloader`.
- Updated metadata, display name, repository URL, and installation path references to match the new plugin name.

## [1.2.0] - 2026-05-04

### Added
- Added `/image <url>` for image downloads.
- Added `gallery-dl` support for general image gallery URLs with dedicated config and cookies file settings.
- Added `ktoolbox` support for Kemono URLs with dedicated `.env` config upload and cookies-to-session extraction.
- Added `image_download_folder` and `image_rclone_folder` settings for image download targets.
- Preserved nested directory structures during image uploads to rclone remotes by transferring the whole downloaded directory tree.

## [1.1.2] - 2026-05-02

### Fixed
- Refreshed the Telegram `/video` and `/audio` inline selection message with the current folder and option summary so button state changes reliably update the visible ✅ marker.
- Made keyboard rendering read the selected folder index from session state by default to keep the visual selection in sync with callback updates.

## [1.1.1] - 2026-04-24

### Fixed
- Fixed rclone progress parsing for carriage-return based progress output.
- Updated Telegram progress delivery to edit a single progress message.
- Stopped interactive sessions before long downloads or rclone transfers so completed tasks no longer emit timeout cancellation messages.
- Throttled progress messages on non-Telegram platforms to reduce message spam.

## [1.1.0] - 2025-03-17

### Added
- Telegram inline keyboard support for `/video` and `/audio` commands
  - Folder selection buttons with visual indicator (✅) for selected folder
  - Config toggle buttons with status indicators (✅/⭕)
    - 存档 (Archive)
    - 代理 (Proxy)
    - 独立文件夹 (Separate folder)
  - Action buttons: 🎬 视频, 🎵 音频, ❌ 取消
- Session-based keyboard state management with unique session IDs

### Technical
- Added `keyboard_session_id` to `SESSION_STATE` for callback tracking
- Platform-aware: keyboard on Telegram, text menu preserved for other platforms

## [1.0.0] - Initial Release

### Features
- Video/audio download using yt-dlp
- Rclone upload support
- Telegram API server support for files >50MB
- Session-based folder and config selection
