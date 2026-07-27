import type { ChatAttachment } from './types';
import type { ChatUploadConfig } from './api';

export const DEFAULT_CHAT_UPLOAD_CONFIG: ChatUploadConfig = {
  max_file_mb: 50,
  max_total_mb: 50,
  max_files_per_message: 5,
  allowed_mime_types: [
    'image/png',
    'image/jpeg',
    'image/webp',
    'image/gif',
    'application/pdf',
  ],
  allowed_extensions: ['.gif', '.jpg', '.jpeg', '.pdf', '.png', '.webp'],
};

export function validateSelectedFiles(
  files: File[],
  pending: ChatAttachment[],
  config: ChatUploadConfig,
  help: string,
): string | null {
  const allowedTypes = new Set(config.allowed_mime_types);
  const allowedExt = new Set(config.allowed_extensions.map((ext) => ext.toLowerCase()));
  const maxFileBytes = config.max_file_mb * 1024 * 1024;
  for (const file of files) {
    const ext = fileExtension(file.name);
    const knownType = file.type ? allowedTypes.has(file.type.toLowerCase()) : false;
    const knownExt = ext ? allowedExt.has(ext) : false;
    if (!knownType && !knownExt) {
      return `${file.name} is not supported. ${help}`;
    }
    if (file.size > maxFileBytes) {
      return `${file.name} exceeds the ${config.max_file_mb} MB per-file limit.`;
    }
  }
  const totalBytes =
    pending.reduce((sum, att) => sum + att.size, 0) +
    files.reduce((sum, file) => sum + file.size, 0);
  if (totalBytes > config.max_total_mb * 1024 * 1024) {
    return `Attachments must stay under ${config.max_total_mb} MB total.`;
  }
  return null;
}

export function uploadErrorMessage(err: unknown, help: string): string {
  const raw = err instanceof Error ? err.message : String(err);
  const table: Record<string, string> = {
    unsupported_type: `Unsupported file type. ${help}`,
    invalid_file_signature: 'The file content does not match its extension.',
    file_too_large: 'The file is larger than the configured upload limit.',
    missing_file: 'No file was received by the server.',
    invalid_session_id: 'Reconnect before attaching files.',
  };
  return table[raw] ?? raw;
}

export function normalizeUploadConfig(cfg: ChatUploadConfig): ChatUploadConfig {
  return {
    max_file_mb: positiveInt(cfg.max_file_mb, DEFAULT_CHAT_UPLOAD_CONFIG.max_file_mb),
    max_total_mb: positiveInt(cfg.max_total_mb, DEFAULT_CHAT_UPLOAD_CONFIG.max_total_mb),
    max_files_per_message: positiveInt(
      cfg.max_files_per_message,
      DEFAULT_CHAT_UPLOAD_CONFIG.max_files_per_message,
    ),
    allowed_mime_types: nonEmptyStrings(
      cfg.allowed_mime_types,
      DEFAULT_CHAT_UPLOAD_CONFIG.allowed_mime_types,
    ),
    allowed_extensions: nonEmptyStrings(
      cfg.allowed_extensions,
      DEFAULT_CHAT_UPLOAD_CONFIG.allowed_extensions,
    ).map((ext) => (ext.startsWith('.') ? ext.toLowerCase() : `.${ext.toLowerCase()}`)),
  };
}

export function describeUploadHelp(config: ChatUploadConfig): string {
  const labels = config.allowed_extensions
    .map((ext) => ext.replace(/^\./, '').toUpperCase())
    .filter((ext, idx, arr) => arr.indexOf(ext) === idx);
  const list = formatList(labels.length ? labels : ['supported files']);
  return `${list}. Max ${config.max_files_per_message} files, ${config.max_total_mb} MB total.`;
}

function fileExtension(name: string): string {
  const idx = name.lastIndexOf('.');
  return idx >= 0 ? name.slice(idx).toLowerCase() : '';
}

function nonEmptyStrings(values: string[] | undefined, fallback: string[]): string[] {
  const clean = Array.isArray(values)
    ? values.map((value) => String(value).trim()).filter(Boolean)
    : [];
  return clean.length ? clean : fallback;
}

function positiveInt(value: number, fallback: number): number {
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

function formatList(values: string[]): string {
  if (values.length <= 1) return values[0] ?? '';
  if (values.length === 2) return `${values[0]} or ${values[1]}`;
  return `${values.slice(0, -1).join(', ')}, or ${values[values.length - 1]}`;
}
