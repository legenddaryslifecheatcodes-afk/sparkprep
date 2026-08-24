"""Print specifications for IngramSpark, KDP and other platforms."""

# Trim sizes in inches (width x height)
TRIM_SIZES = {
    "5x8": {"label": "5\" x 8\"", "w": 5.0, "h": 8.0, "category": "Popular"},
    "5.25x8": {"label": "5.25\" x 8\"", "w": 5.25, "h": 8.0, "category": "Popular"},
    "5.5x8.5": {"label": "5.5\" x 8.5\"", "w": 5.5, "h": 8.5, "category": "Popular"},
    "6x9": {"label": "6\" x 9\" (Most Common)", "w": 6.0, "h": 9.0, "category": "Popular"},
    "6.14x9.21": {"label": "6.14\" x 9.21\"", "w": 6.14, "h": 9.21, "category": "Popular"},
    "7x10": {"label": "7\" x 10\"", "w": 7.0, "h": 10.0, "category": "Non-fiction"},
    "7.5x9.25": {"label": "7.5\" x 9.25\"", "w": 7.5, "h": 9.25, "category": "Non-fiction"},
    "8x10": {"label": "8\" x 10\"", "w": 8.0, "h": 10.0, "category": "Non-fiction"},
    "8.5x11": {"label": "8.5\" x 11\" (Letter)", "w": 8.5, "h": 11.0, "category": "Workbook"},
    "8.25x10.75": {"label": "8.25\" x 10.75\"", "w": 8.25, "h": 10.75, "category": "Trade"},
}

# Paper types with weight in pages-per-inch (PPI) - critical for spine width
PAPER_TYPES = {
    "white_50lb": {"label": "50lb White (Standard B&W)", "ppi": 444, "platforms": ["kdp", "ingramspark"]},
    "cream_50lb": {"label": "50lb Cream (Fiction)", "ppi": 434, "platforms": ["kdp", "ingramspark"]},
    "white_60lb": {"label": "60lb White (Premium)", "ppi": 400, "platforms": ["ingramspark"]},
    "color_60lb_standard": {"label": "60lb Color Standard", "ppi": 460, "platforms": ["kdp"]},
    "color_60lb_premium": {"label": "60lb Color Premium", "ppi": 426, "platforms": ["kdp", "ingramspark"]},
    "groundwood_38lb": {"label": "38lb Groundwood (Novel)", "ppi": 512, "platforms": ["ingramspark"]},
}

# Binding types
BINDING_TYPES = {
    "paperback": {"label": "Paperback (Perfect Bound)", "bleed": 0.125, "safe_margin": 0.375},
    "hardcover_case": {"label": "Hardcover Case Laminate", "bleed": 0.125, "safe_margin": 0.5, "wrap": 0.75},
    "hardcover_jacket": {"label": "Hardcover with Dust Jacket", "bleed": 0.125, "safe_margin": 0.5, "flap": 3.5},
}

# Named CMYK output conditions selectable from the Editor's "Color profile"
# dropdown. NOTE: these are metadata-level (OutputConditionIdentifier/Info
# registry declarations in the PDF's OutputIntent), not embedded binary ICC
# profiles -- the actual .icc profile files for GRACoL/FOGRA/Japan Color are
# distributed under IDEAlliance/Japan Color Committee terms that don't
# clearly permit bundling and redistributing them in a SaaS product, so we
# don't ship them. A well-known registry name without an embedded profile
# is still accepted as PDF/X-1a compliant by pdfx_validator.check_icc_output_intent
# (mirroring how most distributor preflight tools treat it) -- it's the
# correct, legally-safe middle ground until real licensed ICC binaries are
# sourced and bundled.
COLOR_PROFILES = {
    "US Web Coated SWOP v2": {
        "condition_identifier": "CGATS TR 001 (SWOP)",
        "info": "U.S. Web Coated (SWOP) v2",
        "registry": "http://www.color.org",
    },
    "GRACoL 2013": {
        "condition_identifier": "CGATS TR 006 (GRACoL2013)",
        "info": "Coated GRACoL 2013",
        "registry": "http://www.color.org",
    },
    "FOGRA39": {
        "condition_identifier": "FOGRA39L",
        "info": "ISO Coated v2 (FOGRA39)",
        "registry": "http://www.color.org",
    },
    "Japan Color 2001 Coated": {
        "condition_identifier": "JC200103",
        "info": "Japan Color 2001 Coated",
        "registry": "http://www.color.org",
    },
}
DEFAULT_COLOR_PROFILE = "US Web Coated SWOP v2"


# Platform rules
PLATFORMS = {
    "kdp": {
        "name": "Amazon KDP",
        "bleed": 0.125,
        "safe_margin_interior": 0.375,
        "barcode_zone": {"w": 2.0, "h": 1.2},
        "min_page_count": 24,
        "max_page_count": 828,
        "spine_text_min_pages": 79,
        "pdf_standard": "PDF/X-1a:2001",
    },
    "ingramspark": {
        "name": "IngramSpark",
        "bleed": 0.125,
        "safe_margin_interior": 0.5,
        "barcode_zone": {"w": 2.0, "h": 1.2},
        "min_page_count": 18,
        "max_page_count": 1200,
        "spine_text_min_pages": 80,
        "pdf_standard": "PDF/X-1a:2001",
    },
    "barnes_noble": {
        "name": "Barnes & Noble Press",
        "bleed": 0.125,
        "safe_margin_interior": 0.5,
        "barcode_zone": {"w": 2.0, "h": 1.2},
        "min_page_count": 48,
        "max_page_count": 800,
        "spine_text_min_pages": 100,
        "pdf_standard": "PDF/X-1a:2003",
    },
    "lulu": {
        "name": "Lulu",
        "bleed": 0.125,
        "safe_margin_interior": 0.5,
        "barcode_zone": {"w": 2.0, "h": 1.2},
        "min_page_count": 32,
        "max_page_count": 800,
        "spine_text_min_pages": 100,
        "pdf_standard": "PDF/X-1a:2003",
    },
}


def calculate_spine_width(page_count: int, paper_ppi: int) -> float:
    """Calculate spine width in inches based on page count and paper PPI."""
    if page_count <= 0 or paper_ppi <= 0:
        return 0.0
    return round(page_count / paper_ppi, 4)


def calculate_full_cover_dimensions(
    trim_w: float, trim_h: float, spine_w: float, bleed: float, binding: str = "paperback"
) -> dict:
    """Calculate full wrap cover dimensions (back + spine + front + bleed on all sides)."""
    wrap_extra = 0.0
    if binding == "hardcover_case":
        wrap_extra = 0.75  # Case wrap around board edges
    total_w = (trim_w * 2) + spine_w + (bleed * 2) + (wrap_extra * 2)
    total_h = trim_h + (bleed * 2) + (wrap_extra * 2)
    return {
        "total_width": round(total_w, 4),
        "total_height": round(total_h, 4),
        "spine_width": round(spine_w, 4),
        "bleed": bleed,
        "wrap_extra": wrap_extra,
        "front_x": round(bleed + wrap_extra + trim_w + spine_w, 4),
        "back_x": round(bleed + wrap_extra, 4),
        "spine_x": round(bleed + wrap_extra + trim_w, 4),
    }
