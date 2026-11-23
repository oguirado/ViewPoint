"""Judge-only MP4 encoding; PNG pixels remain the evidence."""
from pathlib import Path
from .journal import DriveError


def encode(run, j, frames, execute):
    run = Path(run)
    # Concat handles sparse render indices without silently filling gaps.
    concat = run / "logs" / "frames.concat.txt"
    # Every relative filename is generated from a validated integer, not user text.
    concat.write_text("".join("file '../render/frame_%06d.png'\n" % ri for ri in frames))
    argv = ["/usr/bin/ffmpeg", "-nostdin", "-y", "-r", "%d/%d" % tuple(j["fps"]),
            "-f", "concat", "-safe", "0", "-i", str(concat), "-frames:v", str(len(frames)),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-threads", "1", "-bitexact",
            "-map_metadata", "-1", str(run / "clip.mp4")]
    execute(argv, run / "logs" / "ffmpeg.log", 120, blender=False)
    if not (run / "clip.mp4").is_file():
        raise DriveError("E_DRIVE", "no_output")
    if (run / "clip.mp4").stat().st_size > 20 * 1024 * 1024:
        raise DriveError("E_BUDGET", "mp4_bytes", 6)
