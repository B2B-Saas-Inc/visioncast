#!/usr/bin/env python3
"""Makes a small PowerPoint with speaker notes, for the slides test."""
import sys
from pptx import Presentation

deck = Presentation()
for number, (title, note) in enumerate([("First slide", "Remember to welcome everyone."), ("Second slide", "Mention the launch date."), ("Third slide", "")], start=1):
    slide = deck.slides.add_slide(deck.slide_layouts[0])
    slide.shapes.title.text = title
    slide.placeholders[1].text = f"Page {number}"
    if note:
        slide.notes_slide.notes_text_frame.text = note
deck.save(sys.argv[1])
