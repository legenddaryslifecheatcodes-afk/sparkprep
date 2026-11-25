"""What the Legenddary SparkPrep Author Studio is made of: the book folders, the publishing checklist, the
readiness steps and the launch plan. One place, so the website and the server always agree."""
from __future__ import annotations

import os

# Folders inside every book (trey's list, 2026-11-25). Notes live as text in the book itself, not as files.
FOLDERS = [
    {"key": "manuscript", "label": "Manuscript drafts"},
    {"key": "covers", "label": "Covers and cover revisions"},
    {"key": "interior", "label": "Interior files"},
    {"key": "research", "label": "Research and reference"},
    {"key": "publishing", "label": "Publishing info"},
    {"key": "final", "label": "Final approved versions"},
]
FOLDER_KEYS = {f["key"] for f in FOLDERS}

ALLOWED_EXTS = {".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".md", ".epub",
                ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".psd", ".svg",
                ".xlsx", ".xls", ".csv", ".zip"}

MAX_FILE_MB = int(os.environ.get("STUDIO_MAX_FILE_MB", "250"))
QUOTA_MB = int(os.environ.get("STUDIO_QUOTA_MB", "2048"))          # per member, every version counted
MIN_FREE_DISK_MB = int(os.environ.get("STUDIO_MIN_FREE_DISK_MB", "1500"))   # local disk only: keep room for books
MAX_BOOKS = 50
MAX_NOTES = 200
MAX_NOTE_CHARS = 60_000
MAX_LAUNCH_TASKS = 120
MAX_LEDGER = 500

# The publishing checklist: finished manuscript -> published book. `step` ties each item to a readiness step.
CHECKLIST = [
    {"key": "self_edit", "step": "editing", "label": "Read the whole book again and fix what you find"},
    {"key": "second_reader", "step": "editing", "label": "Have an editor or a trusted reader go through it"},
    {"key": "proofread", "step": "editing", "label": "Proofread every page for typos"},
    {"key": "cover_front", "step": "cover", "label": "Front cover designed"},
    {"key": "cover_back", "step": "cover", "label": "Back cover text and author photo ready"},
    {"key": "cover_spine", "step": "cover", "label": "Spine sized to your final page count"},
    {"key": "trim", "step": "interior", "label": "Pick your book size (trim size)"},
    {"key": "interior_format", "step": "interior", "label": "Lay out the inside pages"},
    {"key": "front_matter", "step": "interior", "label": "Title page, copyright page and dedication"},
    {"key": "isbn", "step": "details", "label": "Get an ISBN (or use the free one from your printer)"},
    {"key": "description", "step": "details", "label": "Write the book description"},
    {"key": "keywords", "step": "details", "label": "Pick categories and keywords"},
    {"key": "price", "step": "details", "label": "Set your price"},
    {"key": "platform", "step": "details", "label": "Choose where to publish (KDP, IngramSpark, Lulu, B&N Press)"},
    {"key": "inspection", "step": "inspection", "label": "Run the full Legenddary SparkPrep check"},
    {"key": "proof_copy", "step": "inspection", "label": "Order a printed proof copy and look it over"},
    {"key": "submitted", "step": "submit", "label": "Submit your book for publishing"},
]
CHECKLIST_KEYS = {c["key"] for c in CHECKLIST}

READINESS_STEPS = [
    {"key": "manuscript", "label": "Manuscript finished"},
    {"key": "editing", "label": "Editing and proofreading"},
    {"key": "cover", "label": "Cover ready"},
    {"key": "interior", "label": "Inside pages formatted"},
    {"key": "details", "label": "ISBN, description and price"},
    {"key": "inspection", "label": "Final technical check"},
    {"key": "submit", "label": "Sent to the printer"},
]

# A starting launch plan, in days before (negative) or after the launch date. Members can edit all of it.
LAUNCH_TEMPLATE = [
    (-90, "plan", "Pick your launch date and write it down"),
    (-75, "plan", "List the people who will help you share the book"),
    (-60, "book", "Cover and inside pages finished"),
    (-60, "marketing", "Make a simple author page or link-in-bio page"),
    (-45, "book", "Order a printed proof copy"),
    (-45, "marketing", "Start an email list (even 10 people counts)"),
    (-30, "social", "Post your cover reveal"),
    (-30, "marketing", "Ask 5 to 10 early readers to read it and leave an honest review after launch"),
    (-21, "social", "Share a short excerpt or behind-the-scenes post"),
    (-14, "book", "Upload your files and set the release date"),
    (-14, "social", "Plan one post a day for launch week"),
    (-7, "marketing", "Email your list: the book comes out next week"),
    (-3, "book", "Check the book page: price, description, categories"),
    (0, "launch", "Launch day: announce it everywhere"),
    (0, "launch", "Thank everyone who helped"),
    (3, "marketing", "Remind early readers about their reviews"),
    (7, "social", "Share a reader's reaction or a photo of the printed book"),
    (14, "marketing", "Look at what sold and what got attention, and plan your next step"),
]
