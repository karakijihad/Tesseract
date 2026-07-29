"""Generated-artifact persistence — mirrors `mirror.server.uploads` for the
return path. Tools that produce files (image_generate, future TTS-to-file,
future video-gen) save here; the chat surface fetches them via
`GET /api/downloads/chat/{session_id}/{artifact_id}/{filename}` and renders
the media inline based on MIME.
"""

from tesseract.mirror.server.downloads._storage import (
    StoredDownload,
    download_file_path,
    load_download,
    save_download,
)

__all__ = [
    "StoredDownload",
    "download_file_path",
    "load_download",
    "save_download",
]
