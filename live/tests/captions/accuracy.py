#!/usr/bin/env python3
"""How well the captions container hears real people: word error rate on recordings of human speech.

Downloads the Open Speech Repository's Harvard sentences (real voices, American and British), and
JFK's inaugural clip from whisper.cpp, sends each to the running captions container, clean and with
noise mixed in, and prints the word error rate. Run with the stack up (tests/e2e/compose.yaml):

    python3 live/tests/captions/accuracy.py --url http://127.0.0.1:18095
"""
import argparse
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.request

HARVARD_1 = ("The birch canoe slid on the smooth planks. Glue the sheet to the dark blue background. It's easy to tell the depth of a well. "
             "These days a chicken leg is a rare dish. Rice is often served in round bowls. The juice of lemons makes fine punch. "
             "The box was thrown beside the parked truck. The hogs were fed chopped corn and garbage. Four hours of steady work faced us. "
             "A large size in stockings is hard to sell.")
HARVARD_2 = ("The boy was there when the sun rose. A rod is used to catch pink salmon. The source of the huge river is the clear spring. "
             "Kick the ball straight and follow through. Help the woman get back to her feet. A pot of tea helps to pass the evening. "
             "Smoky fires lack flame and heat. The soft cushion broke the man's fall. The salt breeze came across from the sea. "
             "The girl at the booth sold fifty bonds.")
JFK = "And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country."
SAMPLES = [
    ("American woman, phone line", "https://www.voiptroubleshooter.com/open_speech/american/OSR_us_000_0010_8k.wav", HARVARD_1),
    ("American man, phone line", "https://www.voiptroubleshooter.com/open_speech/american/OSR_us_000_0011_8k.wav", HARVARD_2),
    ("British man, phone line", "https://www.voiptroubleshooter.com/open_speech/british/OSR_uk_000_0020_8k.wav", HARVARD_1),
    ("JFK, old broadcast", "https://raw.githubusercontent.com/ggerganov/whisper.cpp/master/samples/jfk.wav", JFK),
]


def words(text):
    return re.findall(r"[a-z0-9']+", text.lower().replace("-", " "))


def wer(reference, heard):
    ref, hyp = words(reference), words(heard)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        previous, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (r != h))
    return row[-1] / max(1, len(ref))


def pcm(path, noise_db=None):
    """16 kHz mono 16-bit, what the container takes."""
    graph = ["-af", "aresample=16000"] if noise_db is None else ["-filter_complex", f"anoisesrc=color=pink:amplitude={10 ** (noise_db / 20)}:sample_rate=16000[n];[0:a]aresample=16000[s];[s][n]amix=inputs=2:duration=first:normalize=0"]
    return subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), *graph, "-ac", "1", "-f", "s16le", "-"], check=True, capture_output=True).stdout


def transcribe(url, audio):
    request = urllib.request.Request(f"{url}/transcribe", data=audio, headers={"Content-Type": "application/octet-stream"}, method="POST")
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read())["text"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18095")
    args = parser.parse_args()
    rows = []
    with tempfile.TemporaryDirectory() as folder:
        for name, source, text in SAMPLES:
            path = pathlib.Path(folder) / source.rsplit("/", 1)[1]
            try:
                request = urllib.request.Request(source, headers={"User-Agent": "visioncast-tests"})
                path.write_bytes(urllib.request.urlopen(request, timeout=60).read())
            except OSError as err:
                print(f"skipped {name}: {err}", file=sys.stderr)
                continue
            for label, noise in (("clean", None), ("noisy room", -30), ("very noisy", -20)):
                heard = transcribe(args.url, pcm(path, noise))
                score = wer(text, heard)
                rows.append({"sample": name, "condition": label, "wer": round(score, 3), "heard": heard[:160]})
                print(f"{name:32} {label:12} {score * 100:5.1f}% words wrong   {heard[:70]}")
    if rows:
        print(f"\nAverage: {sum(r['wer'] for r in rows) / len(rows) * 100:.1f}% words wrong across {len(rows)} runs")


if __name__ == "__main__":
    main()
