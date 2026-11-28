# SPDX-License-Identifier: AGPL-3.0-only
"""Build frame-locked effects stems, an editable mix and English captions."""
import wave

import numpy as np

from blocking import resolve
from common import Parser, destination, digest, dump, ffmpeg, project, require, run, slot, stamp

SOUND = "python3 -B sound.py --project PROJECT --out SOUND"
ROLES = ("room", "steps", "switch", "signal")
ANCHORS = {"steps": "first_step", "switch": "causal_press", "signal": "causal_press"}


def cues(docs, block, dialogue):
    sheet = docs["cue-sheet"]
    story = docs["upstream"]["story"]
    total = block["total_edit_frames"]
    rate = sheet["sample_rate"]
    events = block["events"]
    require(rate == story["sample_rate"] and rate % block["fps"] == 0 and sheet["language"] == story["language"],
            "unsupported_audio_clock", "restore the pinned sample rate and caption language", SOUND)
    effects = {row["role"]: row for row in sheet["effects"]}
    require(tuple(effects) == ROLES and len(effects) == len(sheet["effects"]) and set(story["roles"]) == set(ROLES),
            "invalid_cues", "author one effect in each of the declared effect roles", SOUND)
    for role in ROLES:
        row = effects[role]
        start, end = row["start"], row["end"]
        require(type(start) is int and type(end) is int and 0 <= start < end <= total,
                "invalid_cues", "give every sound a valid half-open frame range", SOUND)
        if role in ANCHORS:
            expected = events[ANCHORS[role]]
            require(start >= expected, "early_cue", "move the effect to its causal action frame", SOUND)
            require(row.get("anchor") == ANCHORS[role] and start == expected,
                    "misaligned_cue", "bind the effect to its causal action frame", SOUND)
        else:
            require((start, end) == (0, total), "misaligned_cue", "span the cut with room tone", SOUND)
    captions = [dict(row) for row in sheet["captions"]]
    require(len(captions) == 2 and [row["start"] for row in captions] == [events["first_step"], events["causal_press"]],
            "stale_subtitle", "bind effect captions to the current action frames", SOUND)
    line = sheet.get("dialogue") if dialogue else None
    if dialogue:
        require(line is not None and line["actor"] == block["cast_by_form"]["cart"]
                and line["anchor"] == "reply" and line["start"] == events["reply"] and line["end"] <= total
                and line["caption"] == f'{block["cast"][line["actor"]]["name"].upper()}: {line["line"]}',
                "invalid_dialogue", "align authored dialogue and speaker to the reply", SOUND)
        captions.append({"start": line["start"], "end": line["end"], "text": line["caption"]})
    end = 0
    for row in captions:
        require(type(row["start"]) is int and type(row["end"]) is int and end <= row["start"] < row["end"] <= total
                and isinstance(row["text"], str) and row["text"].strip() == row["text"] and bool(row["text"])
                and "\n" not in row["text"] and "\r" not in row["text"],
                "stale_subtitle", "author nonoverlapping caption intervals on the current edit clock", SOUND)
        end = row["end"]
    return {"sample_rate": rate, "language": sheet["language"], "effects": effects, "captions": captions,
            "dialogue": line, "total_frames": total, "fps": block["fps"]}


def subtitle_text(captions, fps):
    return "".join(f'{i}\n{stamp(row["start"], fps)} --> {stamp(row["end"], fps)}\n{row["text"]}\n\n'
                   for i, row in enumerate(captions, 1))


def write_wav(path, samples, rate):
    require(float(np.max(np.abs(samples))) <= 0.99, "audio_clipped", "lower the stem levels before mixing", SOUND)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(np.rint(samples * 32767).astype("<i2").tobytes())


def effect(role, seconds, start, cuesheet):
    sin = np.sin
    tau = 2 * np.pi
    if role == "room":
        return .013 * sin(tau * 82 * seconds) + .004 * sin(tau * 131 * seconds)
    if role == "steps":
        signal = np.zeros_like(seconds)
        stop = (cuesheet["effects"]["steps"]["end"] - 1 - start) / cuesheet["fps"]
        for offset in (0, stop * .52, stop):
            local = np.maximum(0, seconds - offset)
            signal += (seconds >= offset) * .22 * sin(tau * 119 * local) * np.exp(-44 * local)
        return signal
    if role == "switch":
        return .48 * sin(tau * 1254 * seconds) * np.exp(-62 * seconds)
    attack = np.minimum(seconds * 250, 1)
    return attack * (.22 * sin(tau * 680 * seconds) * np.exp(-4.7 * seconds)
                     + .06 * sin(tau * 1020 * seconds) * np.exp(-3.3 * seconds))


def dialogue_samples(text, rate, count):
    # FFmpeg filter quoting is avoided by constraining the opt-in fixture's spoken line.
    require(isinstance(text, str) and len(text) <= 64 and all(c.isascii() and (c.isalpha() or c in " .?!") for c in text),
            "invalid_dialogue", "use a short ASCII dialogue line for the offline fixture synthesizer", SOUND)
    raw = ffmpeg("-f", "lavfi", "-i", f"flite=text={text}:voice=slt",
                 "-af", "atempo=1.55", "-ar", rate, "-ac", "1", "-f", "s16le", "pipe:1")
    speech = np.frombuffer(raw, dtype="<i2").astype(float) * (.55 / 32768)
    heard = np.flatnonzero(np.abs(speech) > .008)
    require(heard.size > 0, "dialogue_does_not_fit", "supply a line the offline renderer can pronounce", SOUND)
    speech = speech[max(0, int(heard[0]) - rate // 500):]
    require(len(speech) <= count, "dialogue_does_not_fit",
            "shorten the line or extend its frame range", SOUND)
    return np.pad(speech, (0, count - len(speech)))


def write_sound(sheet, root):
    directory = root / "stems"
    directory.mkdir()
    rate, fps = sheet["sample_rate"], sheet["fps"]
    per_frame = rate // fps
    full_mix = np.zeros(sheet["total_frames"] * per_frame)
    refs = {}

    def add(role, start, end, samples):
        track = np.zeros_like(full_mix)
        track[start * per_frame:end * per_frame] = samples
        full_mix[:] += track
        path = directory / f"{role}.wav"
        write_wav(path, track, rate)
        refs[role] = {"path": str(path.relative_to(root)), "sha256": digest(path)}

    for role, row in sheet["effects"].items():
        start, end = row["start"], row["end"]
        seconds = np.arange((end - start) * per_frame) / rate
        add(role, start, end, effect(role, seconds, start, sheet))
    if sheet["dialogue"]:
        row = sheet["dialogue"]
        start, end = row["start"], row["end"]
        add("dialogue", start, end, dialogue_samples(row["line"], rate, (end - start) * per_frame))
    audio = root / "mix.wav"
    write_wav(audio, full_mix, rate)
    subs = root / "captions.en.srt"
    subs.write_text(subtitle_text(sheet["captions"], fps), encoding="utf-8")
    timeline = {"schema": "rp4.editorial-sound/v1", **sheet, "stems": refs,
                "mix": {"path": audio.name, "sha256": digest(audio)},
                "subtitle": {"path": subs.name, "sha256": digest(subs)}}
    dump(root / "sound.json", timeline)
    return timeline


def main():
    parser = Parser(description=__doc__)
    slot(parser)
    parser.add_argument("--dialogue", action="store_true")
    args = parser.parse_args()
    docs = project(args.project)
    sheet = cues(docs, resolve(docs), args.dialogue)
    with destination(args.out) as stage:
        audio = write_sound(sheet, stage)
    return {"status": "SOUND_READY", "stems": list(audio["stems"]), "captions": len(audio["captions"])}


if __name__ == "__main__":
    raise SystemExit(run(main))
