"""Offline MP4 (and GIF) export of the forward simulation.

Frames are rendered headlessly from the engine timeline into a temp directory,
then composed into an MP4 via FFmpeg (browser / QuickTime friendly flags) or a
GIF via Pillow.  The GUI window itself is never screen-recorded.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Callable, Optional

from . import theme
from .engine import SimulationEngine
from .frames import ExportSettings, build_frame_specs
from .renderer import SceneSpec, draw_scene

logger = logging.getLogger(__name__)

ProgressCb = Callable[[int, int, str], None]


class FFmpegNotFoundError(RuntimeError):
    """Raised when no usable FFmpeg binary can be located."""


class VideoExportError(RuntimeError):
    """Raised when frame rendering or FFmpeg composition fails."""


FFMPEG_INSTALL_HINT = (
    "FFmpeg was not found.\n\n"
    "Install one of the following, then retry:\n"
    "  • pip install imageio-ffmpeg   (bundles a private FFmpeg, no admin)\n"
    "  • brew install ffmpeg          (system-wide, macOS Homebrew)\n"
)


def find_ffmpeg() -> Optional[str]:
    """Locate an FFmpeg binary: system PATH first, then bundled imageio one."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:  # pragma: no cover - optional dependency
        logger.debug("imageio_ffmpeg not available", exc_info=True)
    return None


def _render_spec_to_png(spec: SceneSpec, path: str, settings: ExportSettings) -> None:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    import matplotlib.pyplot as plt

    dpi = 100.0
    fig = Figure(figsize=(settings.width / dpi, settings.height / dpi), dpi=dpi,
                 facecolor=theme.BG_CARD)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    fig.subplots_adjust(left=0.02, right=0.98, top=0.92, bottom=0.04)
    draw_scene(ax, spec)
    fig.savefig(path, dpi=dpi, facecolor=theme.BG_CARD)
    plt.close(fig)


def export_animation(
    engine: SimulationEngine,
    output_path: str,
    fmt: str,
    settings: ExportSettings,
    progress: Optional[ProgressCb] = None,
) -> str:
    """Render frames and write ``output_path`` as MP4 or GIF."""
    fmt = fmt.lower()
    if fmt not in {"gif", "mp4"}:
        raise VideoExportError(f"unsupported format {fmt!r}")

    ffmpeg = None
    if fmt == "mp4":
        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            raise FFmpegNotFoundError(FFMPEG_INSTALL_HINT)

    specs = build_frame_specs(engine, settings)
    total = len(specs)
    tmp_dir = tempfile.mkdtemp(prefix="natam_sim_")
    frame_paths: list[str] = []
    try:
        for idx, spec in enumerate(specs):
            fp = os.path.join(tmp_dir, f"frame_{idx:06d}.png")
            _render_spec_to_png(spec, fp, settings)
            frame_paths.append(fp)
            if progress:
                progress(idx + 1, total, "Rendering frames")

        if fmt == "gif":
            _compose_gif(frame_paths, output_path, settings, progress)
        else:
            _compose_mp4(ffmpeg, tmp_dir, output_path, settings, progress)
        return output_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _compose_gif(frame_paths, output_path, settings, progress) -> None:
    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover
        raise VideoExportError("Pillow is required for GIF export.") from exc
    if progress:
        progress(0, 1, "Composing GIF")
    images = [Image.open(fp).convert("P", palette=Image.ADAPTIVE) for fp in frame_paths]
    duration_ms = max(1, round(1000 / settings.fps))
    images[0].save(output_path, save_all=True, append_images=images[1:],
                   duration=duration_ms, loop=0, optimize=True)


def _compose_mp4(ffmpeg, tmp_dir, output_path, settings, progress) -> None:
    if progress:
        progress(0, 1, "Composing MP4")
    cmd = [
        ffmpeg, "-y", "-framerate", str(settings.fps),
        "-i", os.path.join(tmp_dir, "frame_%06d.png"),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise VideoExportError(
            f"FFmpeg failed (exit {proc.returncode}).\n{proc.stderr[-800:]}"
        )
