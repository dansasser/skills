#!/usr/bin/env python3
"""Produce local, source-timed evidence for a supplied file. Uses only Python's standard library."""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import wave
from fractions import Fraction
from pathlib import Path

if __name__ == "__main__":
    # The installed command must not write Python caches beside the skill.
    sys.dont_write_bytecode = True

from tool_config import ConfigError, config_summary, load_config


class MediaError(Exception):
    pass


def write_json(path, value):
    # The index is the last published artifact in each unique run.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def seconds(value):
    value = number(value)
    if value is None or value < 0:
        raise argparse.ArgumentTypeError("Expected finite nonnegative source seconds")
    return value


def step(index, key, status, detail):
    index["steps"][key] = {"status": status, "detail": detail}


class Media:
    def __init__(self, config, source, directory):
        self.config = config
        self.source = source
        self.directory = directory
        self.env = os.environ.copy()
        key = (config["tts"] or {}).get("api_key_env")
        if key:
            self.env.pop(key, None)
        self.origin = 0.0
        self.index = {"version": 1, "source": source.name, "streams": {}, "steps": {}, "frames": []}

    def execute(self, binary, args):
        try:
            run = subprocess.run([binary, *args], cwd=self.directory, env=self.env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        except OSError as exc:
            raise MediaError(f"Could not execute {Path(binary).name}. Check the configured executable.") from exc
        if run.returncode:
            raise MediaError(f"{Path(binary).name} exited with status {run.returncode}.")
        return run.stdout, run.stderr.decode(errors="replace")

    def ffmpeg(self, args):
        return self.execute(self.config["tools"]["ffmpeg"], ["-nostdin", "-hide_banner", "-nostats",
                            "-threads", "2", "-filter_threads", "1", *args])

    def input_args(self):
        return ["-protocol_whitelist", "file,pipe", "-copyts", "-i", str(self.source)]

    def probe(self):
        if not self.source.is_file():
            raise MediaError("The supplied input is not a readable local file.")
        out, _ = self.execute(self.config["tools"]["ffprobe"],
                              ["-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams",
                               "-show_format", "-of", "json", str(self.source)])
        try:
            metadata = json.loads(out)
            streams = metadata["streams"]
        except (ValueError, KeyError, TypeError) as exc:
            raise MediaError("ffprobe did not return a stream inventory.") from exc
        selected = {}
        for kind in ("video", "audio"):
            matches = [s for s in streams if s.get("codec_type") == kind
                       and not (kind == "video" and s.get("disposition", {}).get("attached_pic"))]
            if matches:
                selected[kind] = max(matches, key=lambda s: s.get("disposition", {}).get("default", 0))
        if not selected:
            raise MediaError("No audio or picture stream was found.")
        starts = [number(s.get("start_time")) for s in selected.values()]
        self.origin = number(metadata.get("format", {}).get("start_time"))
        if self.origin is None:
            self.origin = min((v for v in starts if v is not None), default=0.0)
        self.index["playback_origin_pts_seconds"] = self.origin
        for kind, s in selected.items():
            absolute = number(s.get("start_time"))
            start = absolute - self.origin if absolute is not None else None
            duration = number(s.get("duration"))
            self.index["streams"][kind] = {
                "index": s["index"], "codec": s.get("codec_name"), "time_base": s.get("time_base"),
                "start": start, "duration": duration, "end": start + duration if start is not None and duration is not None else None,
                "bounds_method": "stream metadata; null denotes unknown",
            }
            if kind == "video":
                rotation = next((v["rotation"] for v in s.get("side_data_list", []) if "rotation" in v), s.get("tags", {}).get("rotate", 0))
                self.index["streams"][kind].update(width=s.get("width"), height=s.get("height"), rotation=rotation,
                                                    sample_aspect_ratio=s.get("sample_aspect_ratio"), display_aspect_ratio=s.get("display_aspect_ratio"))
        write_json(self.directory / "metadata.json", metadata)
        step(self.index, "probe", "ok", "Selected default audio and picture streams where available. See metadata.json.")

    def visuals(self, mode, selection, interval):
        stream = self.index["streams"].get("video")
        if stream is None:
            step(self.index, "visual", "unavailable", "The input contains no picture stream.")
            return
        before = self.origin
        if mode == "window":
            first, last = selection
            chooser = f"trim=start={first+before:.9f}:end={last+before:.9f}"
        elif mode == "still":
            chooser = f"select='gte(t,{selection[0]+before:.9f})'"
        else:
            elapsed = f"(t-({before:.9f}))"
            previous = f"(prev_t-({before:.9f}))"
            chooser = (f"select='isnan(prev_t)+gt(floor({elapsed}/{interval}),"
                       f"floor({previous}/{interval}))+gt(scene,0.18)'")
        filters = [chooser, "showinfo=checksum=0", f"setpts=PTS-({before:.9f})/TB"]
        if mode != "still":
            filters += ["scale=w='max(2,trunc(min(320,480*dar)/2)*2)':h='max(2,trunc(min(480,320/dar)/2)*2)'",
                        "setsar=1", "pad=320:480:(ow-iw)/2:(oh-ih)/2"]
        raw_dir = self.directory / "frames"
        raw_dir.mkdir()
        args = self.input_args() + ["-map", f"0:{stream['index']}", "-an", "-sn", "-dn"]
        output = ["-enc_time_base:v", "1:25", "-fps_mode", "passthrough", "-threads:v", "1", "-q:v", "2"]
        if mode == "still":
            output += ["-frames:v", "1"]
        filename = "frames/frame-%06d.png" if mode == "still" else "frames/frame-%06d.jpg"
        # Draw timestamps after taking the native PTS. The index also works on builds without drawtext.
        label = r"drawtext=text='%{pts\:hms}':fontsize=16:fontcolor=white:box=1:boxcolor=black@0.75:x=3:y=3"
        # Separate the image encoder clock from the source clock. Some phone recordings
        # have consecutive PTS closer than one JPEG encoder tick. Preserve each image.
        image_clock = ["settb=1/25", "setpts=N"]
        labeled = mode != "still"
        try:
            _, log = self.ffmpeg(args + ["-vf", ",".join(filters + ([label] if labeled else []) + image_clock), *output, filename])
        except MediaError:
            if not labeled:
                raise
            labeled = False
            for partial in raw_dir.glob("frame-*.jpg"):
                partial.unlink()
            _, log = self.ffmpeg(args + ["-y", "-vf", ",".join(filters + image_clock), *output, filename])
        tb = re.search(r"showinfo[^\n]*config in time_base:\s*(\d+/\d+)", log)
        pts = re.findall(r"showinfo[^\n]*?\bn:\s*\d+\s+pts:\s*(-?\d+)\s+pts_time:([-\deE+.]+)", log)
        files = sorted(raw_dir.glob("frame-*"))
        if not files:
            raise MediaError("No image decoded at the requested time or interval.")
        if not pts or len(pts) < len(files) or (mode != "still" and len(pts) != len(files)):
            raise MediaError("Could not pair every decoded image with a source timestamp.")
        time_base = Fraction(tb[1]) if tb else None
        for path, (ticks, display_time) in zip(files, pts):
            absolute = float(int(ticks) * time_base) if time_base is not None else float(display_time)
            self.index["frames"].append({"time": round(absolute - before, 9), "pts": int(ticks),
                "time_base": str(time_base) if time_base is not None else None, "file": path.relative_to(self.directory).as_posix()})
        if mode != "still":
            try:
                self.ffmpeg(["-framerate", "1", "-i", filename, "-vf", "tile=3x3:padding=2:margin=2",
                             "-threads:v", "1", "-q:v", "2", "sheet-%04d.jpg"])
                for i, frame in enumerate(self.index["frames"]):
                    frame.update(sheet=f"sheet-{i//9+1:04d}.jpg", row=(i%9)//3+1, column=i%3+1)
            except MediaError:
                step(self.index, "sheets", "unavailable", "Use the indexed individual frames.")
        self.index["sampling"] = {"mode":mode, "interval_seconds":interval if mode=="inspect" else None,
                                 "requested_seconds":selection, "labels_on_images":labeled}
        step(self.index, "visual", "ok", f"{len(files)} source-timed images.")

    def audio(self, selection=None):
        stream = self.index["streams"].get("audio")
        if stream is None:
            step(self.index, "audio", "unavailable", "The input contains no audio stream.")
            return
        prefix = ""
        if selection:
            lo, hi = selection
            prefix = f"atrim=start={lo+self.origin:.9f}:end={hi+self.origin:.9f},"
        filters = (prefix + "asplit=2[signal][clock];[clock]atrim=end_sample=1,ashowinfo,anullsink;"
                   "[signal]asetpts=PTS-STARTPTS,aresample=16000:async=1:first_pts=0")
        _, log = self.ffmpeg(self.input_args() + ["-map", f"0:{stream['index']}", "-vn", "-sn", "-dn", "-af", filters,
                                                   "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "audio.wav"])
        file = self.directory / "audio.wav"
        try:
            with wave.open(str(file)) as wav:
                duration = wav.getnframes() / wav.getframerate()
        except (OSError, EOFError, wave.Error) as exc:
            raise MediaError("Extracted audio could not be read.") from exc
        match = re.search(r"ashowinfo[^\n]*?\bpts_time:([-\deE+.]+)", log)
        if not match or duration <= 0:
            raise MediaError("No timed audio samples decoded in the requested interval.")
        start = float(match[1]) - self.origin
        span = {"file":"audio.wav","start":round(start,9),"end":round(start+duration,9),
                "duration":duration,"method":"first decoded sample PTS plus WAV sample count"}
        self.index["audio_excerpt"] = span
        if not selection:
            stream.update(start=span["start"], end=span["end"], duration=duration, bounds_method=span["method"])
        step(self.index, "audio", "ok", "Mono 16 kHz PCM is available. Extraction alone does not describe the sound.")

    def transcribe(self):
        asr = self.config["asr"]
        if not self.index.get("audio_excerpt") or not asr or not asr["model_path"] or not Path(asr["model_path"]).is_file():
            step(self.index, "asr", "unavailable", "Timed audio and a configured Whisper.cpp model are required.")
            return
        self.execute(asr["executable"], ["-m", asr["model_path"], "-f", "audio.wav", "-l", asr["language"],
                     "-t", str(asr["threads"]), "-ml", "48", "-sow", "-ovtt", "-osrt", "-oj", "-of", "transcript"])
        try:
            raw = json.loads((self.directory / "transcript.json").read_text(encoding="utf-8"))
            segments = raw["transcription"]
            if not isinstance(segments, list):
                raise ValueError("Expected a list of timed segments")
            span = self.index["audio_excerpt"]
            cues = []
            for i, item in enumerate(segments):
                text = item["text"].strip()
                start = number(item["offsets"]["from"])
                end = number(item["offsets"]["to"])
                if start is None or end is None:
                    raise ValueError("Invalid cue timestamps")
                if not text or text.upper() == "[BLANK_AUDIO]":
                    continue
                lo, hi = max(0.0, start / 1000), min(span["duration"], end / 1000)
                if hi > lo:
                    cues.append({"raw_index":i,"start":round(span["start"]+lo,9),
                                 "end":round(span["start"]+hi,9),"text":text})
            for ext in ("srt", "vtt"):
                if not (self.directory / f"transcript.{ext}").is_file():
                    raise ValueError("Missing raw timed transcript")
        except (KeyError, TypeError, ValueError, OSError, UnicodeError, AttributeError) as exc:
            raise MediaError("Whisper.cpp did not produce a complete, readable set of timed cues.") from exc
        write_json(self.directory / "cues.json", cues)
        step(self.index, "asr", "ok", f"{len(cues)} bounded source-time cues. Raw Whisper outputs have audio-relative times.")

    def attempt(self, kind, action, *args):
        try:
            action(*args)
        except MediaError as exc:
            step(self.index, kind, "failed", str(exc))

    def prepare(self, args):
        self.attempt("probe", self.probe)
        if self.index["steps"]["probe"]["status"] == "failed":
            return False
        mode = args.mode
        selection = getattr(args, "times", None)
        if mode != "audio":
            # Default to a navigable overview on long material, retaining detected changes.
            duration = self.index["streams"].get("video", {}).get("duration") or 0
            interval = getattr(args, "interval", None) or max(1, math.ceil(duration / 120))
            self.attempt("visual", self.visuals, mode, selection, interval)
        if mode == "audio" or (mode == "inspect" and not args.visual_only):
            self.attempt("audio", self.audio, selection)
            if mode == "inspect" and not args.no_asr:
                self.attempt("asr", self.transcribe)
        return any(self.index["steps"].get(key, {}).get("status") == "ok" for key in ("visual", "audio"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Private JSON configuration file")
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("check", help="Inspect tool configuration offline and report credential availability")
    for mode in ("inspect", "window", "still", "audio"):
        cmd = sub.add_parser(mode)
        cmd.add_argument("input", type=Path)
        cmd.add_argument("--out", type=Path, required=True, help="Parent for a fresh evidence directory")
        if mode == "inspect":
            cmd.add_argument("--interval", type=seconds, help="Overview sampling interval in seconds")
            cmd.add_argument("--no-asr", action="store_true", help="Prepare audio without transcription")
            cmd.add_argument("--visual-only", action="store_true", help="Skip audio extraction and ASR")
        else:
            cmd.add_argument("times", type=seconds, nargs=1 if mode == "still" else 2, metavar="SECONDS")
    args = parser.parse_args()
    if getattr(args, "interval", None) == 0:
        parser.error("--interval must be positive")
    times = getattr(args, "times", [])
    if len(times) == 2 and times[1] <= times[0]:
        parser.error("End time must be greater than start time")
    try:
        config = load_config(args.config)
        if args.mode == "check":
            print(json.dumps(config_summary(config), indent=2))
            return 0
        args.out.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="analysis-", dir=args.out.resolve()))
        media = Media(config, args.input.resolve(), directory)
        ok = media.prepare(args)
        write_json(directory / "index.json", media.index)
        print(json.dumps({"index": str(directory / "index.json"), "steps": media.index["steps"]}, ensure_ascii=False))
        return 0 if ok else 2
    except (ConfigError, OSError) as exc:
        print(f"video-analysis: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
