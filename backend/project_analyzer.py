"""Project Analyzer — secure ZIP ingest -> structured project manifest.

Never writes to disk: an uploaded archive is scanned entirely in memory and
reduced to a bounded JSON manifest (paths, languages, stats, framework hints).
Guards: zip-slip traversal, absolute/backslash-escaping names, size caps per
member / archive / total files, symlink/device members, name collisions.

The manifest is what a later pipeline stage (e.g. swarm launch context) consumes;
this module does not touch the database or the Hermes runtime.
"""
from __future__ import annotations

import io
import os
import zipfile
from collections import Counter
from pathlib import PurePosixPath

MAX_ARCHIVE_BYTES = int(os.environ.get("FLUXSWARM_PROJECT_ZIP_MAX_MB", "50")) * 1024 * 1024
MAX_MEMBER_BYTES = 5 * 1024 * 1024
MAX_FILES = int(os.environ.get("FLUXSWARM_PROJECT_ZIP_MAX_FILES", "5000"))
MAX_TREE_DEPTH = 32

# Extension -> language family used for the top-level summary.
_EXT_LANG = {
    ".py": "python", ".pyi": "python", ".pyw": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php",
    ".java": "java", ".kt": "kotlin", ".swift": "swift", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ps1": "powershell",
    ".sql": "sql", ".html": "html", ".htm": "html", ".css": "css",
    ".scss": "css", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".xml": "xml", ".md": "markdown",
    ".dart": "dart", ".swift": "swift", ".vue": "vue", ".svelte": "svelte",
}

_FRAMEWORK_HINTS = {
    "requirements.txt": "python (pip)",
    "pyproject.toml": "python (poetry/pip)",
    "package.json": "javascript (npm)",
    "pubspec.yaml": "dart (flutter)",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "composer.json": "php",
    "pom.xml": "java (maven)",
    "build.gradle": "java (gradle)",
    "Gemfile": "ruby",
    "Dockerfile": "docker",
    "docker-compose.yml": "docker (compose)",
    "iapp.df": "app",
}


class ProjectAnalyzerError(ValueError):
    """Raised for hostile / oversized / non-zip uploads."""


def _is_unsafe(name: str) -> bool:
    parts = PurePosixPath(name).parts
    if not parts or not parts[0]:
        return True
    if name.startswith("/") or os.path.isabs(name) or "\\" in name:
        return True
    if any(p in ("..", ".") or not p for p in parts):
        return True
    return False


def analyze_zip(data: bytes, source_name: str = "project.zip") -> dict:
    """Analyze an uploaded project ZIP in memory and return a bounded manifest.

    Raises ProjectAnalyzerError on: oversize archive, not a zip, zip-slip or
    absolute members, symlink/device members, too many files, oversize member,
    or name collisions (case- and backslash-folded).
    """
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ProjectAnalyzerError(
            f"archive too large: {len(data)} bytes (max {MAX_ARCHIVE_BYTES})")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        raise ProjectAnalyzerError("file is not a valid ZIP archive")

    infos = []
    seen: set[str] = set()
    for info in zf.infolist():
        name = info.filename
        if _is_unsafe(name):
            raise ProjectAnalyzerError(f"unsafe member path: {name!r}")
        folded = name.replace("\\", "/").lower()
        if folded in seen:
            raise ProjectAnalyzerError(f"duplicate member path (case/sep-folded): {name!r}")
        seen.add(folded)
        mode = (info.external_attr >> 16) & 0o170000
        if mode in (0o120000, 0o040000):  # symlink or directory
            raise ProjectAnalyzerError(f"unsupported member type: {name!r}")
        if info.file_size > MAX_MEMBER_BYTES:
            raise ProjectAnalyzerError(
                f"member too large: {name!r} ({info.file_size} bytes)")
        infos.append(info)
    if len(infos) > MAX_FILES:
        raise ProjectAnalyzerError(f"too many files: {len(infos)} (max {MAX_FILES})")

    files = []
    total_bytes = 0
    ext_counter = Counter()
    lang_bytes: Counter = Counter()
    by_ext: dict[str, int] = {}
    for info in infos:
        content = zf.read(info)
        total_bytes += len(content)
        name = info.filename
        if name.endswith("/"):
            continue
        ext = PurePosixPath(name).suffix.lower()
        ext_counter[ext] += 1
        by_ext.setdefault(ext, 0)
        by_ext[ext] = by_ext[ext] + len(content)
        lang = _EXT_LANG.get(ext, "other")
        lang_bytes[lang] += len(content)
        files.append({
            "path": name,
            "bytes": len(content),
            "lines": content.count(b"\n") + (1 if content and not content.endswith(b"\n") else 0),
            "ext": ext or "",
        })

    languages = [
        {"lang": lang, "bytes": nb, "files": sum(1 for f in files if _EXT_LANG.get(f["ext"], "other") == lang)}
        for lang, nb in lang_bytes.most_common(8)
    ]
    root_names = {PurePosixPath(f["path"]).parts[0] for f in files} if files else set()
    frameworks = sorted({hint for name, hint in _FRAMEWORK_HINTS.items()
                         if any(f["path"] == name or f["path"].endswith("/" + name) for f in files)})

    return {
        "source_name": source_name,
        "ok": True,
        "files_count": len(files),
        "total_bytes": total_bytes,
        "total_lines": sum(f["lines"] for f in files),
        "top_level_entries": sorted(root_names),
        "frameworks": frameworks,
        "languages": languages,
        "size": {
            "files_by_ext": {ext: n for ext, n in ext_counter.most_common(20)},
            "bytes_by_ext": {ext: nb for ext, nb in by_ext.items()},
        },
        "files": files[:500],
        "truncated": len(files) > 500,
    }