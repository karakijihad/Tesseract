/**
 * Inline media preview for tool outputs that resolve to a downloads URL.
 *
 * The backend's `/api/downloads/...` route streams the file with the right
 * `Content-Type`, so the only job here is to pick the element type from
 * the extension. Any unknown shape falls through to a plain link.
 *
 * The component is shared across tools — `image_generate` is the first
 * caller, future TTS-to-file / video-gen will plug in unchanged.
 *
 * URL handling: `result.output` is a root-relative `/api/downloads/...`
 * path. In dev the frontend and backend run on different ports, so root-
 * relative URLs resolve against the wrong origin. `backendAssetUrl`
 * absolutizes the path against the resolved BACKEND_BASE.
 */
import { backendAssetUrl } from '../../lib/endpoints';

const DOWNLOAD_URL_RE = /^\/api\/downloads\/chat\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

const IMAGE_EXT = new Set(['.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp']);
const AUDIO_EXT = new Set(['.mp3', '.m4a', '.wav', '.ogg', '.oga', '.flac', '.webm']);
const VIDEO_EXT = new Set(['.mp4', '.webm', '.mov', '.mkv']);
const PDF_EXT = new Set(['.pdf']);

export function isDownloadUrl(value: string): boolean {
  return DOWNLOAD_URL_RE.test(value.trim());
}

function extOf(url: string): string {
  const path = url.split('?')[0];
  const dot = path.lastIndexOf('.');
  return dot === -1 ? '' : path.slice(dot).toLowerCase();
}

interface Props {
  url: string;
  filename?: string;
}

export function DownloadPreview({ url, filename }: Props) {
  const ext = extOf(url);
  const label = filename || url.split('/').pop() || url;
  const src = backendAssetUrl(url);

  if (IMAGE_EXT.has(ext)) {
    return (
      <a href={src} target="_blank" rel="noreferrer" className="dl-preview dl-preview--image">
        <img src={src} alt={label} loading="lazy" />
      </a>
    );
  }
  if (AUDIO_EXT.has(ext)) {
    return (
      <div className="dl-preview dl-preview--audio">
        <audio src={src} controls preload="metadata" />
        <a href={src} target="_blank" rel="noreferrer" className="dl-preview-link">{label}</a>
      </div>
    );
  }
  if (VIDEO_EXT.has(ext)) {
    return (
      <div className="dl-preview dl-preview--video">
        <video src={src} controls preload="metadata" />
        <a href={src} target="_blank" rel="noreferrer" className="dl-preview-link">{label}</a>
      </div>
    );
  }
  if (PDF_EXT.has(ext)) {
    return (
      <div className="dl-preview dl-preview--pdf">
        <object data={src} type="application/pdf" aria-label={label}>
          <a href={src} target="_blank" rel="noreferrer">{label}</a>
        </object>
      </div>
    );
  }
  return (
    <a href={src} target="_blank" rel="noreferrer" className="dl-preview dl-preview--file">
      📎 {label}
    </a>
  );
}
