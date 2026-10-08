"""Where finished tilesets and generated sample videos are kept between runs.

Per-user cache directory (not inside the repository, so `git clean`, a fresh checkout or deleting the project
folder does not throw away hours of encoding). Override with the TILING_CACHE_DIR environment variable.
"""
import hashlib
import os
import shutil
import sys

MARKER = ".tiling-video-decoder-cache"       # only directories carrying this file may be deleted by clear_cache()


def cache_root():
    env = os.environ.get("TILING_CACHE_DIR")
    if env:
        root = env
    elif sys.platform == "darwin":
        root = os.path.expanduser("~/Library/Caches/tiling-video-decoder")
    elif os.name == "nt":
        root = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "tiling-video-decoder", "cache")
    else:
        root = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "tiling-video-decoder")
    marker = os.path.join(root, MARKER)
    if os.path.isdir(root) and not os.path.exists(marker) and os.listdir(root):
        # Stamping the marker here would make clear_cache() treat someone's existing folder as ours and empty it.
        raise RuntimeError("%s already contains files that are not a tiling-video-decoder cache. Point TILING_CACHE_DIR "
                           "at a new or empty directory." % root)
    os.makedirs(root, exist_ok=True)
    if not os.path.exists(marker):
        open(marker, "w").close()
    return root


def tileset_name(video_path, codec, cols, rows, preset):
    """Stable directory name for (this exact video file, these settings). Changes if the file's size or mtime does."""
    st = os.stat(video_path)
    key = "%s|%d|%d" % (os.path.realpath(video_path), st.st_size, st.st_mtime_ns)
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in os.path.splitext(os.path.basename(video_path))[0])[:40]
    return "%s-%s-%dx%d-%s-%s" % (stem, codec, cols, rows, preset, hashlib.sha1(key.encode()).hexdigest()[:8])


def size_mb(path):
    total = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(dirpath, f)
            if not os.path.islink(fp):
                total += os.path.getsize(fp)
    return total / 2**20


def clear_cache(root=None):
    """Deletes the cache, but only a directory this tool created (it must contain the marker file)."""
    root = root or cache_root()
    if not os.path.exists(os.path.join(root, MARKER)):
        raise RuntimeError("%s is not a tiling-video-decoder cache; refusing to delete it" % root)
    freed = size_mb(root)
    for name in os.listdir(root):
        if name != MARKER:
            p = os.path.join(root, name)
            shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
    return freed
