"""Project Analyzer — secure ZIP ingest -> structured project manifest + codebase snapshot.

The manifest is what a later pipeline stage (e.g. swarm launch context) consumes;
this module does not touch the database or the Hermes runtime.

Two-phase output:
  1. Metadata manifest (paths, languages, stats, framework hints) — cheap, read once.
  2. Codebase snapshot — truncated source content of key files, fed to agents so
     they can plan/improve against the REAL code rather than guessing blind.
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from collections import Counter
from pathlib import PurePosixPath

MAX_ARCHIVE_BYTES = int(os.environ.get("FLUXSWARM_PROJECT_ZIP_MAX_MB", "50")) * 1024 * 1024
MAX_MEMBER_BYTES = 5 * 1024 * 1024
MAX_FILES = int(os.environ.get("FLUXSWARM_PROJECT_ZIP_MAX_FILES", "5000"))
MAX_TREE_DEPTH = 32

# ---- Codebase snapshot limits (agent context budget) ------------------------
# Bounded so the total injected context stays under ~60 KB (~15K tokens).
_SNAPSHOT_MAX_BYTES_TOTAL = 60 * 1024
_SNAPSHOT_MAX_LINES_PER_FILE = 150
_SNAPSHOT_MAX_FILES = 40

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


# ---- Source file selection for the codebase snapshot -------------------------

# Files most useful to agents (entry points, config, core logic).
_PRIORITY_NAMES = {
    "main.py", "app.py", "server.py", "index.py", "manage.py", "run.py",
    "wsgi.py", "asgi.py", "settings.py", "config.py", "models.py",
    "views.py", "routes.py", "handlers.py", "controller.py", "controllers.py",
    "serializers.py", "schema.py", "schemas.py", "tasks.py", "celery.py",
    "models/", "views/", "routes/", "controllers/", "handlers/", "core/",
    "src/", "lib/", "api/", "app/", "tests/", "test/",
    "package.json", "tsconfig.json", "vite.config.js", "vite.config.ts",
    "next.config.js", "next.config.js", "nuxt.config.js",
    "requirements.txt", "pyproject.toml", "setup.py", "Cargo.toml",
    "go.mod", "Gemfile", "pubspec.yaml", "composer.json", "pom.xml",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".env.example", "README.md", "CHANGELOG.md",
}

_CODE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".rb", ".php", ".java", ".kt", ".swift",
    ".c", ".h", ".cpp", ".cc", ".hpp", ".cs",
    ".sh", ".bash", ".sql", ".html", ".htm", ".css", ".scss",
    ".json", ".yaml", ".yml", ".toml", ".xml", ".dart", ".vue", ".svelte",
}

_CONFIG_NAMES = {
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "package.json", "tsconfig.json", "vite.config.js", "vite.config.ts",
    "next.config.js", "nuxt.config.js", "Cargo.toml", "go.mod",
    "Gemfile", "pubspec.yaml", "composer.json", "pom.xml",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "Makefile", "CMakeLists.txt", ".env.example", "Procfile",
}


def _file_priority_score(path: str) -> int:
    """Higher = more valuable to agents. Config files beat source; entry
    points beat helpers; top-level beats nested."""
    name = PurePosixPath(path).name
    parts = PurePosixPath(path).parts
    depth = len(parts)
    score = max(0, 20 - depth)  # top-level gets 20, deeper gets less
    if name in _CONFIG_NAMES:
        score += 100
    if name in _PRIORITY_NAMES or name.lower() in {n.lower() for n in _PRIORITY_NAMES}:
        score += 80
    ext = PurePosixPath(path).suffix.lower()
    if ext in _CODE_EXTS:
        score += 30
    if name in ("__init__.py",):
        score -= 10
    return score


def extract_codebase_snapshot(zf: zipfile.ZipFile, files: list[dict]) -> dict:
    """Extract a bounded codebase snapshot from a ZIP for agent context.

    Returns:
        {
            "file_tree": "project/\n├── src/\n│   ├── app.py\n│   └── ...",
            "key_files": [{"path": str, "content": str}],  # truncated source
            "config_summary": str,  # concatenated config files (Dockerfile, package.json, etc.)
            "total_source_bytes": int,
        }

    All output is bounded by _SNAPSHOT_MAX_BYTES_TOTAL to keep prompt injection safe.
    """
    # Build sorted file tree
    all_paths = sorted(f["path"] for f in files)
    file_tree = _build_file_tree(all_paths)

    # Select files to extract: configs first, then high-priority source, then
    # remaining source files — all capped at _SNAPSHOT_MAX_FILES.
    scored = [(_file_priority_score(f["path"]), f) for f in files]
    scored.sort(key=lambda x: -x[0])
    selected = scored[:_SNAPSHOT_MAX_FILES]

    key_files = []
    config_parts = []
    total_src = 0

    for _score, finfo in selected:
        if total_src >= _SNAPSHOT_MAX_BYTES_TOTAL:
            break
        path = finfo["path"]
        if path.endswith("/"):
            continue
        try:
            raw = zf.read(path)
        except Exception:
            continue
        ext = PurePosixPath(path).suffix.lower()
        name = PurePosixPath(path).name

        # Decode text files only
        if ext not in _CODE_EXTS and name not in _CONFIG_NAMES:
            continue
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            continue
        # Truncate per-file
        lines = text.splitlines(keepends=True)[:_SNAPSHOT_MAX_LINES_PER_FILE]
        truncated_flag = len(text.splitlines()) > _SNAPSHOT_MAX_LINES_PER_FILE
        content = "".join(lines)
        if truncated_flag:
            content += f"\n... [{len(lines)} of {finfo.get('lines', len(lines))} lines shown]\n"

        chunk_bytes = len(content.encode("utf-8"))
        if total_src + chunk_bytes > _SNAPSHOT_MAX_BYTES_TOTAL:
            break
        total_src += chunk_bytes

        if name in _CONFIG_NAMES:
            config_parts.append(f"--- {path} ---\n{content.strip()}")
        else:
            key_files.append({"path": path, "content": content.strip()})

    config_summary = "\n\n".join(config_parts)[:8000]

    return {
        "file_tree": file_tree,
        "key_files": key_files,
        "config_summary": config_summary,
        "total_source_bytes": total_src,
    }


def _build_file_tree(paths: list[str], max_entries: int = 120) -> str:
    """Build a compact text file tree from a sorted path list."""
    if not paths:
        return "(empty project)"
    tree_lines: list[str] = []
    prev_parts: list[str] = []
    shown = 0
    for p in paths:
        if shown >= max_entries:
            tree_lines.append(f"  ... ({len(paths) - shown} more files)")
            break
        parts = PurePosixPath(p).parts
        # Only show unique directory structure
        depth = len(parts) - 1
        indent = "  " * depth
        name = parts[-1] if parts else p
        tree_lines.append(f"{indent}{name}")
        shown += 1
    return "\n".join(tree_lines)


def _is_unsafe(name: str) -> bool:
    parts = PurePosixPath(name).parts
    if not parts or not parts[0]:
        return True
    # Drive-letter absolute (``C:\...`` / ``C:/...``) is absolute on every OS,
    # not just ntpath; posixpath.isabs would let it through on Linux.
    if re.match(r"^[A-Za-z]:", name):
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

    # Extract codebase snapshot so agents can see real source content.
    try:
        snapshot = extract_codebase_snapshot(zf, files)
    except Exception:
        snapshot = {"file_tree": "", "key_files": [], "config_summary": "", "total_source_bytes": 0}

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
        "codebase_snapshot": snapshot,
    }