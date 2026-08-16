"""Offline animation export: render timeline frames, then compose GIF / MP4.

The GUI window is *never* screen-recorded.  Instead each frame is rendered
headlessly from the operation timeline, written to a temp directory, and either
assembled into a GIF (Pillow) or an MP4 via FFmpeg.

MP4 uses FFmpeg with browser/QuickTime/PowerPoint-friendly flags::

    ffmpeg -y -framerate <fps> -i frame_%06d.png \
        -c:v libx264 -pix_fmt yuv420p -movflags +faststart output.mp4
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional

from . import theme
from .renderer import SceneSpec, draw_scene
from .state import GuiState, Site, instruction_atoms, op_kind

logger = logging.getLogger(__name__)

ProgressCb = Callable[[int, int, str], None]


class FFmpegNotFoundError(RuntimeError):
    """Raised when no usable FFmpeg binary can be located."""


class VideoExportError(RuntimeError):
    """Raised when frame rendering or FFmpeg composition fails."""


@dataclass
class ExportSettings:
    fps: int = 30
    width: int = 1280
    height: int = 720
    hold_seconds: float = 0.6      # dwell time on each discrete step
    move_seconds: float = 0.7      # duration of a single move animation

    @property
    def hold_frames(self) -> int:
        return max(1, round(self.hold_seconds * self.fps))

    @property
    def move_frames(self) -> int:
        return max(2, round(self.move_seconds * self.fps))


def find_ffmpeg() -> Optional[str]:
    """Locate an FFmpeg binary: system PATH first, then the bundled one."""
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


FFMPEG_INSTALL_HINT = (
    "FFmpeg was not found.\n\n"
    "Install one of the following, then retry:\n"
    "  • pip install imageio-ffmpeg   (bundles a private FFmpeg, no admin needed)\n"
    "  • brew install ffmpeg          (system-wide, macOS Homebrew)\n"
)


# --------------------------------------------------------------------- frames
def build_frame_specs(state: GuiState, settings: ExportSettings) -> list[SceneSpec]:
    """Expand the operation timeline into per-video-frame scene specs."""
    frames: list[SceneSpec] = []
    total_ops = len(state.operations)

    init_positions = state.positions_at(0)
    init_frame = SceneSpec(
        positions=init_positions,
        title="Initial atom layout",
        subtitle=f"{len(init_positions)} atoms",
    )
    frames.extend([init_frame] * settings.hold_frames)

    for i, inst in enumerate(state.operations):
        kind = op_kind(inst)
        label = f"Step {i + 1}/{total_ops}: {kind}"
        before = state.positions_at(i)
        after = state.positions_at(i + 1)

        if kind == "MOVE":
            endpoints = state.move_endpoints(i)
            n = settings.move_frames
            for f in range(n):
                progress = f / (n - 1) if n > 1 else 1.0
                moves = [(a, src, dst, progress) for a, src, dst in endpoints]
                frames.append(
                    SceneSpec(
                        positions=before,
                        highlight={a for a, _, _ in endpoints},
                        moves=moves,
                        title=label,
                        subtitle="Atom movement (hardware schedule, not a gate)",
                    )
                )
            frames.extend([SceneSpec(positions=after, title=label)] * max(1, settings.hold_frames // 2))
            continue

        highlight = instruction_atoms(inst)
        entangle_pairs: list[tuple[int, int]] = []
        subtitle = ""
        if kind == "2Q":
            for g in inst.get("gates", []):
                entangle_pairs.append((int(g["q0"]), int(g["q1"])))
            subtitle = "Two-qubit entangling gate"
        elif kind == "SWAP":
            qs = inst.get("qubits", [])
            if len(qs) >= 2:
                entangle_pairs.append((int(qs[0]), int(qs[1])))
            subtitle = "Explicit quantum SWAP"
        elif kind == "1Q":
            subtitle = "Single-qubit gate"
        elif kind == "MEASURE":
            subtitle = "Measurement"

        spec = SceneSpec(
            positions=after,
            highlight=highlight,
            entangle_pairs=entangle_pairs,
            title=label,
            subtitle=subtitle,
        )
        frames.extend([spec] * settings.hold_frames)

    if not frames:
        frames.append(SceneSpec(positions=init_positions, title="Empty schedule"))
    return frames


def _render_spec_to_png(spec: SceneSpec, path: str, settings: ExportSettings) -> None:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    dpi = 100.0
    fig = Figure(
        figsize=(settings.width / dpi, settings.height / dpi),
        dpi=dpi,
        facecolor=theme.BG_CARD,
    )
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    fig.subplots_adjust(left=0.02, right=0.98, top=0.92, bottom=0.04)
    draw_scene(ax, spec)
    fig.savefig(path, dpi=dpi, facecolor=theme.BG_CARD)
    # release
    import matplotlib.pyplot as plt

    plt.close(fig)


def export_animation(
    state: GuiState,
    output_path: str,
    fmt: str,
    settings: ExportSettings,
    progress: Optional[ProgressCb] = None,
) -> str:
    """Render frames and write ``output_path`` as GIF or MP4.

    Returns the output path on success; raises on failure.
    """
    fmt = fmt.lower()
    if fmt not in {"gif", "mp4"}:
        raise VideoExportError(f"unsupported format {fmt!r}")

    ffmpeg = None
    if fmt == "mp4":
        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            raise FFmpegNotFoundError(FFMPEG_INSTALL_HINT)

    specs = build_frame_specs(state, settings)
    total = len(specs)
    tmp_dir = tempfile.mkdtemp(prefix="rc_frames_")
    frame_paths: list[str] = []
    try:
        for idx, spec in enumerate(specs):
            frame_path = os.path.join(tmp_dir, f"frame_{idx:06d}.png")
            _render_spec_to_png(spec, frame_path, settings)
            frame_paths.append(frame_path)
            if progress:
                progress(idx + 1, total, f"Rendering frame {idx + 1}/{total}")

        if fmt == "gif":
            _compose_gif(frame_paths, output_path, settings)
        else:
            _compose_mp4(frame_paths, output_path, settings, ffmpeg, progress, total)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return output_path


def _compose_gif(frame_paths: list[str], output_path: str, settings: ExportSettings) -> None:
    from PIL import Image

    if not frame_paths:
        raise VideoExportError("no frames to write")
    duration_ms = int(1000 / settings.fps)
    images = [Image.open(p).convert("P", palette=Image.ADAPTIVE) for p in frame_paths]
    images[0].save(
        output_path,
        save_all=True,
        append_images=images[1:],
        duration=duration_ms,
        loop=0,
        optimize=True,
    )


def _compose_mp4(
    frame_paths: list[str],
    output_path: str,
    settings: ExportSettings,
    ffmpeg: str,
    progress: Optional[ProgressCb],
    total: int,
) -> None:
    if not frame_paths:
        raise VideoExportError("no frames to write")
    frame_dir = os.path.dirname(frame_paths[0])
    if progress:
        progress(total, total, "Encoding MP4 with FFmpeg…")
    cmd = [
        ffmpeg,
        "-y",
        "-framerate", str(settings.fps),
        "-i", os.path.join(frame_dir, "frame_%06d.png"),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        # Ensure even dimensions for yuv420p compatibility.
        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        output_path,
    ]
    logger.info("FFmpeg command: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise VideoExportError(
            "FFmpeg failed (exit "
            f"{proc.returncode}).\n\nCommand:\n{' '.join(cmd)}\n\n"
            f"stderr:\n{proc.stderr[-2000:]}"
        )
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise VideoExportError("FFmpeg reported success but produced no output file.")
