# Changelog

All notable changes to this project will be documented in this file.

## [1.4.0] - 2026-08-11

### Added
- Added `video_config.cookie_browser` to load yt-dlp cookies from a browser profile on the AstrBot host.
- Added support and documentation for absolute browser profile paths, including Playwright Chromium profiles.

### Changed
- Routed yt-dlp and rclone output through the plugin logger and added process, proxy, cookie-source, exit-code, and extractor diagnostics.
- Added network and fragment retries, socket timeouts, and resumable `.part` downloads for transient proxy failures.

### Fixed
- Collected yt-dlp errors after the process exits instead of stopping at the first `ERROR` line.
- Prevented Telegram menu events from immediately cancelling video downloads after they are consumed.
- Redacted temporary media URL query parameters and proxy credentials from diagnostic logs.

## [1.3.1] - 2026-07-26

### Fixed
- Stopped the downloader hanging the bot when gallery-dl, ktoolbox, or yt-dlp produced no output for an extended period: subprocess reads now time out (60s idle / 1200s total) and the process is terminated.
- Isolated downloader subprocess stdin to `/dev/null` so misbehaving tools cannot block on the parent's stdin.
- Made `/stop` effective against active `/video`, `/audio`, and `/image` downloads by periodically checking the event stop flag inside the download loops and terminating the subprocess when cancellation is requested.
- Passed `--abort 1` to gallery-dl and detected repeated identical output lines so gallery-dl no longer keeps the bot stuck in an internal auth/retry loop when login info is missing; the subprocess is terminated and a failure is reported promptly.

## [1.3.0] - 2026-07-25

### Added
- Added configurable per-download Clash proxy-group node selection with in-memory session state and automatic restoration of the original node.

### Fixed
- Streamed yt-dlp progress from both standard output and standard error while preserving rate-limited progress delivery.

## [1.2.9] - 2026-07-25

### Fixed
- Routed Telegram downloader button callbacks through the active interactive session, preventing callback result decoration errors and post-download timeout messages.
- Started `yt-dlp` downloads without a blocking title preflight so Bilibili progress and failures are reported promptly.

## [1.2.8] - 2026-07-11

### Fixed
- Consumed Telegram text and file replies handled by the interactive downloader session so folder selections and other menu input no longer trigger an LLM response.

## [1.2.7] - 2026-07-09

### Changed
- Telegram text and file replies now delete the user's reply when possible and refresh the existing interactive menu message instead of appending new menu messages.

## [1.2.6] - 2026-05-05

### Fixed
- Added `--remote-components ejs:github` to all `yt-dlp` invocations so YouTube EJS challenge solver scripts can be fetched when Node-based JS challenge solving is required.

## [1.2.5] - 2026-05-05

### Fixed
- Passed uploaded cookies and proxy settings to the `yt-dlp --get-title` probe so authenticated links no longer fail before download starts.
- Moved the target URL to the end of the `yt-dlp` command to keep later options such as `--cookies` applied reliably.
- Made plugin file-path resolution accept both list and string config values for uploaded files.

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
