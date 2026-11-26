---
name: video-analysis
description: Examine a supplied video, analyze a reel or a particular cut, explain how visuals and sound work together, or compare multiple reels to identify their editing style. Use for grounded creative assessments, timed visual details and style comparisons.
license: MIT
---

# Video Analysis

Start with the media the caller supplied. Keep the source intact. Focus on the editor’s question, whether it concerns one moment, a complete edit or a pattern across several clips. Locating media, making a new video and generating standalone narration are separate jobs.

## Prepare evidence

Run [the preparation helper](scripts/prepare_video.py) with Python 3. Resolve its location relative to this SKILL.md. FFmpeg and ffprobe handle media. Whisper.cpp and a compatible model handle speech. The helper prints the path to a fresh `index.json` whose artifact paths are relative to its directory. Keep the index and the images, audio and transcripts it identifies together.

Use the supplied private settings whenever the caller identifies them. `--config` precedes the subcommand. It overrides `VIDEO_ANALYSIS_CONFIG`. With neither, local FFmpeg and ffprobe are selected on PATH. For a new configuration, copy [the example](config.example.json) to a private location and set the executable names or paths, model and language. Relative paths resolve beside that configuration. A bare executable name uses PATH. For TTS, set the endpoint including its route prefix, the model and the **name** of the credential variable. Its value comes from the process environment or the specified dotenv file. `check` reports local configuration and key availability offline. Examining a recording needs no synthesis request.

The following commands assume `helper`, `settings`, `clip` and `output` hold the paths to the helper, private JSON file, supplied video and evidence directory.

```sh
python3 "$helper" --config "$settings" inspect "$clip" --out "$output"
python3 "$helper" --config "$settings" window "$clip" 3.0 4.0 --out "$output"
python3 "$helper" --config "$settings" still "$clip" 5.1 --out "$output"
python3 "$helper" --config "$settings" audio "$clip" 3.15 3.65 --out "$output"
python3 "$helper" --config "$settings" check
```

- For a whole recording, start with `inspect`. Open the sheets and the time-aligned cues. The overview takes periodic samples and includes detected scene changes between them. Use `--interval SECONDS` to change the overview cadence. Sheets read left to right, top to bottom. The index gives source times even on FFmpeg builds without printed labels.
- For a question about a cut, go directly to `window`. It decodes consecutive source frames over the half-open interval. Compare the last frame before the change, its first frame, and the first restored or subsequent frame to bound its duration. To reopen an indexed frame at native resolution, pass its integer `pts` to `still INPUT --pts TICKS --out DIR` using the same input video. If starting from a playback time, `still INPUT SECONDS --out DIR` selects the first frame at or after that time. Quote the actual time in its index.
- For a question about sound, use `audio` for the relevant span. The WAV begins at the source time in `audio_excerpt.start`. Inspect it with a listening capability or measure the feature in question. A level spike can establish a change in level. To identify a tone by frequency, measure periodicity over the actual passage. State what was heard or measured. With picture-only access, restrict the finding to the picture.
- `inspect --no-asr` prepares audio without transcribing. `inspect --visual-only`, `window` and `still` inspect picture. On a partial result, read `steps` in the index and continue with usable evidence. Mark each unavailable modality. A failed input in a batch keeps its own label and outcome.

## Examine the edit

1. **Inspect the images.** Open the sheets with the host’s image viewer, including all sheets that cover the question. Locate the hook, explanatory changes, emphasis beats and ending where they exist. Distinguish platform overlays and playback loops from the edit. If the image viewer is unavailable, disclose that limit. Producing image files alone cannot establish what is in them.
2. **Separate the layers.** For each meaningful beat, identify what holds steady and what changes. Look at composition, presenter, camera or framing, headline, lower captions, diagrams, colors, typography and graphic reveals. Open a full-sized still for a qualifier, proper name or small counter.
3. **Verify timing and movement.** Reinspect abrupt changes and gaps between periodic samples. In a comparison, check unusual shots in each clip before declaring a layout continuous or an effect shared. Use consecutive frames to distinguish a hard insert, a gradual transition and a single sampled pose. Report a time range when timing is approximate.
4. **Align words and sound.** Use `cues.json` for playback times. Raw `transcript.json`, SRT and VTT use the extracted audio clock. Audio can begin later or continue after the final picture. A blank marker supplies no spoken words. Compare likely mishearings against speech captions, visible labels or a replay. Preserve raw text. Record a supported correction with its raw cue index, original and reviewed wording, source time and basis. Keep an uncertain reading tentative. A headline can summarize an idea without matching the speaker word for word.
5. **Explain the choice.** Connect a specific observation to a plausible editorial function. For example, a steady speaker panel can keep the viewer oriented while graphics above it change with each instruction. Read any qualifier before interpreting a number. Distinguish an illustrated or spoken claim from verified performance.

For **one recording**, lead with the answer, then give a compact source-timed timeline or the requested moment. Link the specific evidence file or indexed frame when it helps the editor revisit a finding. Mention coverage limits that materially affect the answer.

For **several recordings**, examine each separately before synthesizing. For each recurring technique give a labeled source moment in every clip said to exhibit it. Explain how different graphic treatments can serve the same editorial purpose. Identify variations and one-offs, and say which clips support them. The final style description should tell the editor what to look for in another reel.
