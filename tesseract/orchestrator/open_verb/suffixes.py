"""One suffix vocabulary, two questions.

`classify` asks "is this string file-shaped?" and `resolve` asks "which
renderer draws it?" — different questions over the same nouns. They were
maintained separately and had already drifted, so the sets live here and each
module derives what it needs.
"""

from __future__ import annotations

IMAGE = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"})
VIDEO = frozenset({".mp4", ".webm", ".mov", ".m4v", ".ogv"})
AUDIO = frozenset({".mp3", ".wav", ".ogg", ".oga", ".flac", ".m4a", ".aac"})
TABLE = frozenset({".csv", ".tsv"})
MARKDOWN = frozenset({".md", ".markdown"})
HTML = frozenset({".html", ".htm"})
CODE = frozenset(
    {
        ".txt", ".log", ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go",
        ".java", ".rb", ".c", ".h", ".cpp", ".hpp", ".cs", ".sh", ".yaml",
        ".yml", ".toml", ".ini", ".cfg", ".json", ".xml", ".css", ".sql",
    }
)

LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript",
    ".jsx": "jsx", ".rs": "rust", ".go": "go", ".java": "java", ".rb": "ruby",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".sh": "bash", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".json": "json", ".xml": "xml", ".css": "css", ".sql": "sql",
}

# Files whose contents are secrets. A card is persisted to canvas state and
# read by whoever is looking at the Mirror, so rendering one is disclosure —
# `open` refuses them outright rather than choosing a renderer.
SECRET_NAMES = frozenset(
    {
        ".env", ".netrc", "_netrc", ".npmrc", ".pypirc", ".htpasswd",
        ".git-credentials", ".dockercfg", "credentials", "secrets",
        "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "known_hosts",
        ".pgpass", ".my.cnf", ".s3cfg", ".boto", "shadow",
    }
)
SECRET_SUFFIXES = frozenset(
    {
        ".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".ppk",
        ".asc", ".gpg", ".kdbx", ".ovpn", ".crt", ".csr", ".der",
    }
)


def is_secret(name: str) -> bool:
    """A denylist, and denylists are never complete — this cannot promise that
    no credential ever reaches a card. What it does is stop the shapes that
    actually sit in a home directory. The residual is recorded in
    `Docs/Deferred.md` rather than implied away.
    """
    lowered = name.lower()
    if lowered in SECRET_NAMES or lowered.startswith(".env"):
        return True
    return any(lowered.endswith(suffix) for suffix in SECRET_SUFFIXES)


# Everything the cockpit can draw. `classify` uses this to tell a filename from
# a hostname, so it must include every renderable suffix — a set that drifts
# short of the renderers is how `report.docx` starts resolving as a domain.
RENDERABLE = IMAGE | VIDEO | AUDIO | TABLE | MARKDOWN | HTML | CODE | frozenset({".pdf"})
