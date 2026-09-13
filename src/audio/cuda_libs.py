from __future__ import annotations

import os
import sys
from pathlib import Path

_READY = False


def nvidia_bin_dirs() -> list[Path]:
    roots: list[Path] = []
    for entry in sys.path:
        p = Path(entry) / "nvidia"
        if p.is_dir():
            roots.append(p)
    bins: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for bin_dir in root.glob("*/bin"):
            resolved = bin_dir.resolve()
            if resolved in seen or not resolved.is_dir():
                continue
            seen.add(resolved)
            bins.append(resolved)
        for bin_dir in root.glob("*/lib/x64"):
            resolved = bin_dir.resolve()
            if resolved in seen or not resolved.is_dir():
                continue
            seen.add(resolved)
            bins.append(resolved)
    return bins


def ensure_cuda_dlls() -> list[str]:
    """Put pip-installed CUDA 12 DLLs (cublas/cudnn/cudart) on the Windows search path."""
    global _READY
    added: list[str] = []
    bins = nvidia_bin_dirs()
    if not bins:
        _READY = True
        return added
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    for folder in reversed(bins):
        folder_s = str(folder)
        if sys.platform == "win32":
            try:
                os.add_dll_directory(folder_s)
            except (OSError, FileNotFoundError, AttributeError):
                pass
        if folder_s not in path_parts:
            path_parts.insert(0, folder_s)
            added.append(folder_s)
    os.environ["PATH"] = os.pathsep.join(path_parts)
    _READY = True
    return added
