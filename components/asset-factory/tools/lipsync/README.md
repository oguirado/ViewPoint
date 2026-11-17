# Offline lip sync

`tools.lipsync.build_track` accepts signed little-endian PCM WAV bytes and optional ElevenLabs timing JSON. A direct timing object or a wrapper with `alignment` and `normalized_alignment` is valid. The normalized spoken text takes precedence when present. A malformed normalized object is refused. A null normalized value permits raw alignment. If both are null, the alignment entry point refuses. Call the WAV entry point with Python `None` for the explicit jaw-only envelope mode.

Case and Latin accent folding feed the generic rig's mouth shapes. A decomposed base with combining marks works within an entry. A separately supplied combining mark must immediately follow the prior entry on the timing clock. Unnormalized digits and unsupported symbols remain neutral. The adapter uses the supplied normalized spoken expansion when one exists.

Character times are rounded to the nearest source sample, half up. Alignment and Rhubarb cues are clipped to the PCM activity window with a rate-scaled bridge of up to five milliseconds [TARGET]. A fully silent bilabial closure is retained when above-threshold PCM brackets it and the entire intervening quiet gap is at most 150 ms [TARGET]. `inactive_aligned_spans` lists cues dropped for lack of activity support. Each frame considers cues overlapping its forward interval and chooses a complete bilabial closure first, then a non-rest cue, then the longest overlap. A tie uses the earlier cue. Adjacent articulated shapes can blend at the sampling anchor. A closure that spans less than a frame still gets a complete pose when supported. Output cadence limits the number of distinguishable events. The timing witness evaluates labelled silence on complete held frame intervals for these tracks. The jaw envelope samples at frame PTS.

## ElevenLabs response media

`tools.lipsync.media.build_elevenlabs_track` takes the full response and its declared `output_format` and returns the track and the exact WAV bytes used by the track. It decodes `audio_base64`. An explicit `pcm_<rate>` carries headerless mono signed 16-bit little-endian PCM [CITED provider format]. The adapter wraps the bytes in WAV and uses the declared rate. The full response SHA-256 is the alignment identity. The derived WAV SHA-256 is the playback and witness identity.

For timed `mp3` or `mp3_<rate>_<bitrate>`, pass the admitted local `ffmpeg` and `ffprobe` executables and a timeout. `decode_mp3_timed` can also be called directly to retain the trim evidence. It verifies the contiguous MP3 packet sample clock and an explicit positive skip on the first packet and discard on the last packet. FFmpeg decodes the same seekable input at its native sample rate and applies the gapless trim. Admission checks the decoded WAV rate, channels and sample count against the packet calculation. Missing or inconsistent trim produces `mp3_timing_unproven`. Keep the MP3, decoded WAV and packet probe digests together. The trim proof establishes the local decoder clock. An independent timing witness establishes whether the provider alignment shares that clock. `decode_mp3_diagnostic` permits local inspection without a trim claim.

## Rhubarb trial

`tools.lipsync.rhubarb.build_rhubarb_track` accepts PCM WAV bytes and explicit executable, language and timeout. English language tags select `pocketSphinx`. Other tags select `phonetic`. Use `und` only for a language-unspecified diagnostic. The adapter runs the local binary with one thread [TARGET], through `asset_factory.proc` under the assigned job limits. The FFmpeg tools use the same process boundary and the local file protocol.

Rhubarb mouth cue mappings are proposed generic rig targets [TARGET].

| Rhubarb cue | Generic rig |
|---|---|
| A | closed |
| B, C, H | wide |
| D | open |
| E, F | round |
| G | bite |
| X | rest |

`track_from_cues` applies the same mapping to saved JSON. Both paths return the common `af.viseme-track/v1` track. Rhubarb's `alignment_sha256` hashes the ordered `mouthCues` array with the original cue letters and exact decimal times before sample rounding. The hash uses compact JSON, sorted cue keys, normalized decimal numbers and a trailing newline. Metadata and temporary paths are outside this digest. Retain the raw JSON to preserve metadata. Rhubarb production recognition and the generic pose approximations require separate held-out review.
