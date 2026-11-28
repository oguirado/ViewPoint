# SPDX-License-Identifier: AGPL-3.0-only
"""Prepare a blind playback packet and validate a returned observation card."""
from pathlib import Path
import shutil

from common import Parser, destination, digest, dump, load, require, run

QUESTIONS = [
    {"id": "actor_count", "prompt": "How many distinct characters can you track through both views?",
     "values": ["two", "other", "cannot_see"]},
    {"id": "eyeline", "prompt": "Do the characters keep their screen direction at the cut?",
     "values": ["consistent", "flipped", "cannot_see"]},
    {"id": "cause_order", "prompt": "Does the relay respond before, at, or after the switch is pressed?",
     "values": ["before", "at-or-after", "cannot_see"]},
    {"id": "audio_sync", "prompt": "Does the relay sound coincide with the visual response?",
     "values": ["aligned", "early-or-late", "cannot_see"]},
    {"id": "captions", "prompt": "With English captions enabled, do they identify the audible events as they occur?",
     "values": ["aligned", "stale-or-missing", "cannot_see"]},
]
EXPECTED = {"actor_count": "two", "eyeline": "consistent", "cause_order": "at-or-after",
            "audio_sync": "aligned", "captions": "aligned"}
JUDGE = "python3 -B panel.py judge --packet PACKET --card CARD"


def prepare(bundle, out):
    movie = bundle / "master.mp4"
    receipt = load(bundle / "receipt.json")
    sha = digest(movie)
    require(sha == receipt["conform"]["sha256"], "stale_picture", "prepare the panel from the sealed delivery")
    with destination(out) as stage:
        shutil.copyfile(movie, stage / "cut.mp4")
        dump(stage / "job.json", {"schema": "rp4.editorial-panel/v1", "cut": "cut.mp4", "sha256": sha,
                                  "playback": "Watch once at normal speed with audio and English captions enabled.",
                                  "questions": QUESTIONS})
    return {"status": "READY_FOR_DEPTH_0", "sha256": sha, "questions": len(QUESTIONS)}


def judge(packet, card_path):
    job = load(packet / "job.json")
    card = load(card_path)
    require(job["schema"] == "rp4.editorial-panel/v1" and job["questions"] == QUESTIONS
            and job["cut"] == "cut.mp4" and digest(packet / "cut.mp4") == job["sha256"]
            and card["schema"] == "rp4.editorial-observation/v1" and card["sha256"] == job["sha256"],
            "stale_panel", "match the observation card to the untouched review clip", JUDGE)
    answers = card["answers"]
    require(set(answers) == set(EXPECTED) and all(
        answers[q["id"]] in q["values"] for q in QUESTIONS)
            and isinstance(card["observer"], str) and bool(card["observer"].strip())
            and isinstance(card["notes"], str) and bool(card["notes"].strip()),
            "invalid_observation", "return one supported answer per question with observer and notes", JUDGE)
    require("cannot_see" not in answers.values(), "cannot_see", "replay the same clip with captions and audio", JUDGE)
    require(answers == EXPECTED, "observation_mismatch", "review the reported continuity or timing failure", JUDGE)
    return {"status": "MATCHED_OBSERVATION", "sha256": job["sha256"], "observer": card["observer"]}


def main():
    parser = Parser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    stage = sub.add_parser("prepare")
    stage.add_argument("--bundle", type=Path, required=True)
    stage.add_argument("--out", type=Path, required=True)
    review = sub.add_parser("judge")
    review.add_argument("--packet", type=Path, required=True)
    review.add_argument("--card", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "prepare":
        return prepare(args.bundle, args.out)
    return judge(args.packet, args.card)


if __name__ == "__main__":
    raise SystemExit(run(main))
