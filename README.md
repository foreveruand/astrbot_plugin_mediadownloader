# AstrBot Media Downloader Plugin

A video/audio/image downloader plugin for AstrBot using `yt-dlp[node]` and gallery-dl, with optional external `ktoolbox` command support for Kemono downloads.

## Features

- Download videos from YouTube, Bilibili, Twitter, and many other platforms
- Audio-only download mode
- Image download mode for gallery sites and Kemono URLs
- Download progress display
- Editable Telegram progress updates and throttled progress messages on other platforms
- Configurable download folders
- Optional rclone upload support
- Proxy support
- Uploaded cookies are applied to both metadata lookup and the actual yt-dlp download
- Read yt-dlp cookies directly from a local browser profile
- The plugin enables `yt-dlp` EJS remote components via `--remote-components ejs:github` for YouTube challenge solving
- Download archive to avoid re-downloading
- Telegram file upload support (download files directly)
- Optional separate folder per video
- Real-time yt-dlp progress updates with rate-limited message edits
- Per-download Clash proxy-group node selection and automatic restoration
- Image downloads verify that gallery-dl created new files before reporting success

## Installation

1. Place the plugin folder in `data/plugins/astrbot_plugin_mediadownloader/`
2. Install dependencies:
   ```bash
  pip install yt-dlp[node] gallery-dl httpx
   ```
   `yt-dlp[node]` installs the Node.js runtime used by this plugin via `--js-runtimes node`.
3. If you need Kemono downloads, install `ktoolbox` separately so the `ktoolbox` command is available in `PATH`.
   ```bash
   pip install ktoolbox
   ```
4. Restart AstrBot

## Configuration

Configure the plugin in the AstrBot admin panel:

The settings are grouped into second-level sections:

- `common_config`
  Enables shared behaviors such as download archive.
- `video_config`
  Contains download folders, cookies, proxy, separate-folder layout, and optional Clash node switching. Configure `clash_controller_url`, `clash_proxy_group`, and `clash_nodes` together to enable Clash selection.
- `image_config`
  Contains local image download path plus `gallery-dl` / external `ktoolbox` config and cookies files.
- `rclone_config`
  Contains shared upload switch, remote name, video remote folders, and image remote folder.

## Usage

### Basic Download

```
/video <url>
```

Example:
```
/video https://www.youtube.com/watch?v=xxxxx
```

### Audio Download

```
/audio <url>
```

### Image Download

```
/image <url>
```

Behavior:
- Kemono URLs use `ktoolbox`
- Other supported image/gallery URLs use `gallery-dl`
- Local mode saves to `image_download_folder`
- rclone mode downloads to a temp directory and uploads the whole tree to `image_rclone_folder`, preserving nested paths such as `author/platform/...`
- A successful gallery-dl process with no new files is reported as a download failure. Check the plugin DEBUG log when the URL requires cookies, authentication fails, the network request fails, or the download archive skips an already-downloaded file.

### Telegram File Download

```
/video <filename>
```

Then upload a file in Telegram. The plugin will download the file to the selected directory.

### Interactive Selection

After sending a URL, you can:
1. Reply with a number (1, 2, 3...) to select a download directory
2. Reply "存档" to toggle archive option
3. Reply "代理" to toggle proxy option
4. Reply "独立文件夹" to toggle per-video folder layout
5. Reply `Clash节点 <序号>` to choose a configured Clash node when enabled
6. Reply "视频" to download video
7. Reply "音频" to download audio only
8. Reply "开始" to download using the default mode

On Telegram, inline button clicks and text/file replies use the same interactive session, refresh the existing menu message when possible, and stop the session when a download begins. Configured Clash nodes appear in one button row, with the current group node selected by default when it is in the configured list. User reply messages are deleted when Telegram permits it and are consumed by the downloader session, so they do not trigger an LLM response.

### Browser Cookies and Diagnostics

Set `video_config.cookie_browser` to read cookies from a browser on the AstrBot host. Leave it empty to use the uploaded Netscape-format `cookie_file`; when both are configured, the browser source is used. The browser profile must be readable by the user running AstrBot.
For X/Twitter image posts, configure `image_config.gallery_dl_cookie_file` with an exported cookie file when guest access returns no results. A gallery-dl process can exit with code `0` without downloading anything when the post is unavailable to guests.

Examples:

```text
chromium
chromium:Profile 1
chromium:/root/.config/google-chrome-for-testing/Profile 1
firefox:default-release
```

The plugin records the yt-dlp executable, cookie source, proxy, process ID, exit code, and extractor errors in the plugin log. Set the plugin log level to `DEBUG` in the AstrBot dashboard for yt-dlp verbose diagnostics. Cookie values, proxy credentials, and temporary media URL parameters are redacted.

## Supported Platforms

yt-dlp supports 1000+ sites including:
- YouTube
- Bilibili
- Twitter/X
- TikTok
- Instagram
- Vimeo
- And many more...

gallery-dl supports a wide range of image gallery sites, while ktoolbox covers Kemono creator/post downloads.

## Requirements

- yt-dlp[node] (installed system-wide or via pip)
- gallery-dl
- ktoolbox command (optional, only needed for Kemono downloads)
- FFmpeg (for audio extraction and video merging)

## License

MIT License
