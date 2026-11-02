#!/usr/bin/env python3
"""Watches a shared folder for decks and turns each one into page images.

The service makes <spool>/<job>/in.<ext>, then the file `ready`. This writes page-<n>.png for
each page and finally done.json: {"pages": n, "notes": [...]} or {"error": "..."}. Documents come
from hosts and producers, but they're still documents: this runs with no network, a read-only
disk and no privileges (see deploy/live/compose.yaml), and each step has a time limit.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

SPOOL = pathlib.Path(os.environ.get("SLIDES_SPOOL", "/spool"))
MAX_PAGES = 500
WIDTH = 1920


def notes_of(path, pages):
    if path.suffix.lower() != ".pptx":
        return []
    from pptx import Presentation

    notes = []
    for slide in Presentation(str(path)).slides:
        frame = slide.notes_slide.notes_text_frame if slide.has_notes_slide else None
        notes.append(frame.text.strip() if frame is not None else "")
    return (notes + [""] * pages)[:pages]


def convert(job):
    deck = next(p for p in job.iterdir() if p.name.startswith("in."))
    with tempfile.TemporaryDirectory() as work:
        work = pathlib.Path(work)
        if deck.suffix.lower() == ".pdf":
            pdf = deck
        else:
            subprocess.run(
                ["soffice", "--headless", "--norestore", f"-env:UserInstallation=file://{work}/profile",
                 "--convert-to", "pdf", "--outdir", str(work), str(deck)],
                check=True, timeout=150, capture_output=True, env={**os.environ, "HOME": str(work)},
            )
            pdf = work / (deck.stem + ".pdf")
        subprocess.run(["pdftoppm", "-png", "-scale-to-x", str(WIDTH), "-scale-to-y", "-1", "-l", str(MAX_PAGES), str(pdf), str(work / "page")],
                       check=True, timeout=150, capture_output=True)
        pages = sorted(work.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[1]))
        if not pages:
            raise ValueError("no pages")
        for number, page in enumerate(pages, start=1):
            shutil.move(str(page), job / f"page-{number}.png")
    return {"pages": len(pages), "notes": notes_of(deck, len(pages))}


def main():
    print(f"watching {SPOOL}")
    while True:
        try:
            waiting = list(SPOOL.glob("*/ready"))
        except OSError as err:  # A folder it can't read must not stop the next deck either.
            print(f"could not read the spool: {type(err).__name__}", file=sys.stderr)
            waiting = []
        for ready in waiting:
            job = ready.parent
            try:
                ready.unlink()
                result = convert(job)
            except Exception as err:  # A bad deck must not stop the next one.
                print(f"{job.name}: {type(err).__name__}", file=sys.stderr)
                result = {"error": "could not convert"}
            try:
                partial = job / "done.part"
                partial.write_text(json.dumps(result))
                partial.rename(job / "done.json")
            except OSError:  # The service gave up and took the folder away.
                pass
        time.sleep(0.5)


if __name__ == "__main__":
    main()
