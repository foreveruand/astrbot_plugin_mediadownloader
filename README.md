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
- Download archive to avoid re-downloading
- Telegram file upload support (download files directly)
- Optional separate folder per video

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
  Contains `yt-dlp` related options like download folders, cookies, proxy, and separate-folder layout.
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
5. Reply "视频" to download video
6. Reply "音频" to download audio only
7. Reply "开始" to download using the default mode

On Telegram, the inline keyboard message will refresh the current folder and option summary after each click so the visible selection marker stays in sync with the effective download settings.

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
