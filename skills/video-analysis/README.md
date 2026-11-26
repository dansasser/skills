# Video Analysis

Examine a finished reel and explain how the edit works. Find the frame where a graphic appears, check a caption against the speaker’s words, or compare several reels to identify an editing style. Each finding points back to a moment you can replay.

If Video Analysis is useful to you, [⭐ star the repo on GitHub](https://github.com/dansasser/skills).

For one clip, the agent can give you a short timeline or answer a question about a single cut. For several clips, it cites an example from each reel that uses a recurring technique and identifies variations. The helper prepares frames, audio and speech cues for the agent to examine. [Agent instructions](SKILL.md)

## Requirements

- The helper uses the Python standard library and has been tested on Linux with Python 3.10 and 3.14.
- FFmpeg and ffprobe with support for the supplied media. The agent needs a readable input file, a writable location for the evidence and an image viewer. Findings about sound require listening to the passage or measuring the feature in question.
- For speech transcription, a local `whisper-cli` executable from whisper.cpp and a compatible model. These are optional for visual analysis and audio extraction.

## Install

Run the command for your agent in the project where you want to use the skill. These commands select Video Analysis from a published collection that contains it. To install from a local checkout, use the path to that checkout in place of `dansasser/skills`. The pinned Skills CLI requires Git and Node.js 22.20 or newer.

```sh
# Codex
npx skills@1.9.0 add dansasser/skills --skill video-analysis --agent codex --copy

# Claude Code
npx skills@1.9.0 add dansasser/skills --skill video-analysis --agent claude-code --copy
```

For a manual project install, copy the complete `video-analysis` folder into `.agents/skills/` for Codex or `.claude/skills/` for Claude Code. Open a fresh session in that project. The collection guide also covers user-wide installs. [Installation guide](https://github.com/dansasser/skills/blob/main/docs/installation.md)

## Use with your own reels

Supply the files along with the question you want answered. These examples assume the reels are in your project directory.

- **Codex** — `$video-analysis In reel-a.mp4, when does the headline change? Show me the frames on either side of the cut.`
- **Claude Code** — `/video-analysis Compare reel-a.mp4 and reel-b.mp4. Which visual and sound choices give them a shared style? Give me a timestamp in each reel for every shared choice.`

## Configure the tools

With default settings, the helper finds `ffmpeg` and `ffprobe` on `PATH`. To customize them or enable transcription, copy the example JSON to a private workspace. Set the tool paths, the whisper.cpp executable and model file, and the speech language. Bare executable names use `PATH`. Relative file paths resolve beside your JSON file. [Example configuration](config.example.json)

Pass `--config` **before** the subcommand, or set `VIDEO_ANALYSIS_CONFIG` to your private JSON file. The command-line setting takes precedence. From the folder containing `SKILL.md`, run `python3 scripts/prepare_video.py --config path/to/settings.json check` to see which tools and model are available.

The optional text-to-speech settings take a provider base URL, model and the **name** of the API-key variable. The key comes from the process environment or the private dotenv file named by `env_file`. The `check` command reports key availability offline. Examining a recording requires no TTS call or API key.

## Quick start

This example makes a two-second clip with a steady tone, a fixed cyan bar and a yellow card that appears briefly above it. Run it from the collection checkout root. For a project install, set `SKILL_DIR` to the installed skill folder instead.

```sh
SKILL_DIR=skills/video-analysis
helper="$SKILL_DIR/scripts/prepare_video.py"
demo=$(mktemp -d)

ffmpeg -nostdin -hide_banner -loglevel error \
  -f lavfi -i "color=c=navy:s=192x320:r=12:d=2" \
  -f lavfi -i "sine=frequency=440:duration=2" \
  -vf "drawbox=x=16:y=260:w=160:h=20:color=cyan:t=fill,drawbox=x=24:y=44:w=144:h=132:color=yellow:t=fill:enable='between(n,9,11)'" \
  -c:v mpeg4 -q:v 3 -c:a aac "$demo/quick-cut.mp4"

python3 "$helper" inspect "$demo/quick-cut.mp4" --no-asr --out "$demo/evidence"
python3 "$helper" window "$demo/quick-cut.mp4" 0.65 1.10 --out "$demo/evidence"
```

## Verification

Each command prints a JSON result with the path to its own `index.json`. In the first result, `probe`, `visual` and `audio` should each report `ok`. Open the overview sheet and play the adjacent `audio.wav` to hear the continuous tone.

Open the second result’s sheet and read its six frames left to right, top to bottom. The yellow card first appears at **0.750 s** and is gone at **1.000 s**. It occupies three frames, a quarter-second of emphasis, while the cyan bar stays in place. The `frames` entries give the playback time, image file and sheet position for each observation.

## Troubleshooting

| What you see | What to check |
| --- | --- |
| A brief insert or small caption is hard to read | Use `window` for consecutive frames across the cut. Use `still` at one of the indexed times for a native-resolution image. |
| The picture is usable but a transcript is missing | Read `steps.asr` in the index and run `check` with your private settings. Verify the whisper.cpp executable and model. Use `inspect --no-asr` to keep the audio or `inspect --visual-only` for picture alone. |
| The spoken line seems late or early | Read `cues.json` for playback times. The raw subtitles use the extracted audio clock. For a closer listen, `audio` extracts a selected interval and records its start time in the index. |

Keep generated evidence in your workspace. Frame sheets can contain faces or captions, transcripts contain speech, and `metadata.json` can include the input file’s full path.

## License

**Dan Sasser** licenses the code, instructions, documentation and example configuration in this folder under the [MIT License](LICENSE). FFmpeg, whisper.cpp, model weights and supplied recordings retain their own terms.
