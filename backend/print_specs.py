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
# Values below are IngramSpark's numbers, taken directly from their own File
# Creation Guide (Cover Setup: Casebound / Dust Jacket, and the Custom Trim
# Sizes bleed formulas) -- a real rejection on a real hardcover-jacket
# submission ("BIND TYPE/TRIM SIZE OF THE JACKET FILE ... DOES NOT MATCH THE
# METADATA", "INSUFFICIENT OR NO BLEED ON COVER LAYOUT") is what caught this.
# The file that got rejected had none of this: calculate_full_cover_dimensions()
# had no jacket-specific branch at all and silently built a plain paperback-
# shaped wrap, ~7" narrower than a real jacket (missing both 3.25" flaps and
# their 0.25" hinges) -- exactly a bind-type mismatch, not a bleed tweak.
# Case laminate genuinely uses 0.625" bleed (a "wrap" that folds around the
# board); dust jacket uses the ordinary 0.125" bleed plus its own flap/hinge
# geometry -- these are NOT interchangeable despite both being "hardcover".
#
# These act as the fallback/default spec (labels always come from here) --
# see PLATFORM_BINDING_OVERRIDES below for platforms confirmed to compute
# hardcover differently. Do NOT assume these numbers are correct for a
# platform that hasn't been separately verified: KDP's real Print Cover
# Calculator output for a real 6x9/76-page/cream hardcover proved its wrap,
# board-size adjustment, AND spine formula are all different from
# IngramSpark's -- there is no evidence any of this generalizes across
# distributors, each apparently keeps its own internal spec.
BINDING_TYPES = {
    "paperback": {"label": "Paperback (Perfect Bound)", "bleed": 0.125, "safe_margin": 0.375},
    "hardcover_case": {
        "label": "Hardcover Case Laminate",
        "bleed": 0.625,
        "safe_margin": 0.5,
        "gutter_hinge": 0.5,  # gap between each cover panel and the spine (guide: "0.5\" GUTTER / HINGE")
        # The board a case-laminate cover wraps is NOT the trim size itself --
        # it's narrower (a hardcover board sits slightly inside the page
        # block's width) and taller (the board overhangs top/bottom, the
        # "square"). Confirmed exactly against a real rejection: for this
        # book's real 6x9 / spine 0.313" numbers, this formula reproduces
        # IngramSpark's stated required cover size (14.194 x 10.5) to the
        # third decimal.
        "board_width_adjust": -0.185,
        "board_height_adjust": 0.25,
    },
    "hardcover_jacket": {
        "label": "Hardcover with Dust Jacket",
        "bleed": 0.125,
        "safe_margin": 0.5,
        "flap": 3.25,  # guide: "Dust jackets have an additional 3.25\" area that wraps around the hardcover book"
        "wrap_fold": 0.25,  # guide: "0.25\" (6mm) strip that connects the front and back covers to the dust jacket flaps"
    },
}

# Per-(platform, binding) numeric overrides for platforms confirmed to use
# different hardcover math than the IngramSpark-sourced defaults above.
# Anything not listed here falls back to BINDING_TYPES unchanged.
#
# KDP hardcover_case: reverse-engineered from KDP's own live Print Cover
# Calculator (Hardcover / Black & White / Cream paper / 6x9 / 76 pages),
# which returned Full Cover 13.954" x 10.417", Front Cover 6.197" x 9.236",
# Wrap 0.591", Spine 0.379". That reproduces to the third decimal as:
#   total_w = 2*wrap + 2*(trim_w + board_width_adjust) + spine_w
#   total_h = 2*wrap + (trim_h + board_height_adjust)
# with NO separate additive gutter/hinge term -- KDP's "Hinge" marker sits
# inside the front/back cover zone rather than adding to it, unlike
# IngramSpark's separate 0.5" gutter/hinge. Only verified at this one trim
# size/page count/paper combination -- treat as a strong estimate, not a
# second guaranteed-exact formula, until cross-checked at another size.
# KDP's calculator also confirmed hardcover spine is its own internal
# table too (76 pages/cream gave 0.379", nothing close to page/PPI), so the
# manual spine_width_override applies here exactly as it does for IngramSpark.
#
# KDP does not offer a distinct "dust jacket" binding at all -- its cover
# calculator's Binding type list is just Hardcover / Paperback, no jacket
# option -- so hardcover_jacket is listed in PLATFORM_UNSUPPORTED_BINDINGS
# below rather than given KDP numbers here.
PLATFORM_BINDING_OVERRIDES = {
    "kdp": {
        "hardcover_case": {
            "bleed": 0.591,
            "gutter_hinge": 0.0,
            "board_width_adjust": 0.197,
            "board_height_adjust": 0.236,
        },
    },
    # Lulu's own Book Creation Guide states plainly: "We print all book
    # files with a bleed margin of 0.125 in ... for all sides" -- no
    # special hardcover wrap like IngramSpark/KDP -- and its cover file
    # spec is a single integrated spread with no board-size adjustment or
    # separate gutter/hinge zone described anywhere. Lulu hardcover is
    # genuinely shaped like a plain wrap, just with its own spine source
    # (see LULU_HARDCOVER_SPINE_TABLE / calculate_spine_width_for_platform).
    "lulu": {
        "hardcover_case": {
            "bleed": 0.125,
            "gutter_hinge": 0.0,
            "board_width_adjust": 0.0,
            "board_height_adjust": 0.0,
        },
    },
}

# (platform, binding) combinations that platform doesn't actually offer as a
# real submission type, distinct from "we don't have confirmed numbers for
# it yet" -- selecting this combo isn't just an estimate, it's not a thing
# that platform's own tools let you submit.
PLATFORM_UNSUPPORTED_BINDINGS = {
    "kdp": {"hardcover_jacket"},
    "lulu": {"hardcover_jacket"},  # Lulu's binding options are Paperback / Hardcover only -- no jacket
}

# Lulu publishes its spine formulas outright rather than hiding them behind
# a proprietary calculator (Book Creation Guide, "Spine Width Calculations"):
#   Paperback: (page_count / 444) + 0.06in -- a FLAT constant regardless of
#     paper stock (unlike IngramSpark/KDP, which vary PPI by paper type).
#     SparkPrep's generic calculate_spine_width(page_count, paper_ppi) is
#     missing this +0.06in entirely for Lulu paperbacks specifically.
#   Hardcover: a stepped lookup table, not a formula (thresholds are
#     page-count RANGES, upper-bound inclusive per the guide's own table).
LULU_PAPERBACK_SPINE_CONSTANT = 0.06
LULU_HARDCOVER_SPINE_TABLE = [
    (23, None), (84, 0.25), (140, 0.5), (168, 0.625), (194, 0.688), (222, 0.75),
    (250, 0.813), (278, 0.875), (306, 0.938), (334, 1.0), (360, 1.063), (388, 1.125),
    (416, 1.188), (444, 1.25), (472, 1.313), (500, 1.375), (528, 1.438), (556, 1.5),
    (582, 1.563), (610, 1.625), (638, 1.688), (666, 1.75), (694, 1.813), (722, 1.875),
    (750, 1.938), (778, 2.0), (799, 2.063), (800, 2.125),
]


def calculate_spine_width_for_platform(
    page_count: int, paper_ppi: int, platform: str = "ingramspark", binding: str = "paperback",
) -> tuple[float, bool]:
    """Spine width plus whether it's a confirmed-exact figure (vs a plain
    page/PPI estimate that should be verified against the distributor's own
    calculator for a hardcover binding). Only Lulu has a publicly documented
    formula/table for both bindings; IngramSpark and KDP hardcover spine
    comes from an internal calculator with no public formula -- for those,
    this still returns the page/PPI estimate, flagged as unconfirmed, and
    the project's spine_width_override is how the user supplies the real
    number.
    """
    if platform == "lulu":
        if binding == "hardcover_case":
            for upper, width in LULU_HARDCOVER_SPINE_TABLE:
                if page_count <= upper:
                    # Below 24 pages Lulu's own table is "N/A" -- hardcover
                    # isn't offered at that page count at all, not a 0-width spine.
                    return (width if width is not None else 0.0), width is not None
            # Past 800 pages the published table simply stops (also Lulu's
            # own max_page_count) -- returning the last row's width as if it
            # were still exact would be a guess dressed up as a fact.
            return LULU_HARDCOVER_SPINE_TABLE[-1][1], False
        return round(page_count / 444, 4) + LULU_PAPERBACK_SPINE_CONSTANT, True

    estimate = calculate_spine_width(page_count, paper_ppi)
    is_confirmed = binding not in ("hardcover_case", "hardcover_jacket")
    return estimate, is_confirmed


def resolve_binding_spec(binding: str, platform: str = "ingramspark") -> dict:
    """BINDING_TYPES entry for `binding`, merged with any platform-specific
    numeric override. Labels/flags not overridden come from the default."""
    base = dict(BINDING_TYPES.get(binding, BINDING_TYPES["paperback"]))
    override = PLATFORM_BINDING_OVERRIDES.get(platform, {}).get(binding)
    if override:
        base.update(override)
    return base

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
    trim_w: float, trim_h: float, spine_w: float, bleed: float, binding: str = "paperback",
    platform: str = "ingramspark",
) -> dict:
    """Calculate full wrap cover dimensions (back + spine + front + bleed on all sides).

    Paperback is a plain wrap. Case laminate and dust jacket are NOT --
    each has its own bleed value and its own extra panels (a gutter/hinge
    for case laminate, flaps + their own hinge for a jacket). The default
    numbers are IngramSpark's, taken verbatim from their File Creation Guide
    (Cover Setup: Casebound / Dust Jacket + the Custom Trim Sizes bleed
    formulas, p.24-27 & 42):

      Case laminate: board_w = trim_w - 0.185, board_h = trim_h + 0.25
                      bleed_w = 2*bleed + 2*gutter_hinge + 2*board_w + spine_w
                      bleed_h = 2*bleed + board_h
      Dust jacket:    bleed_w = 2*bleed + 2*wrap_fold + 2*flap + 2*trim_w + spine_w
                      bleed_h = 2*bleed + trim_h

    `platform` selects a PLATFORM_BINDING_OVERRIDES entry when one exists
    (currently KDP's hardcover_case, reverse-engineered from KDP's own Print
    Cover Calculator -- confirmed to use a different wrap, a different board
    adjustment, AND no separate gutter/hinge term at all, i.e. genuinely
    different math, not just different numbers). The resolved spec's own
    "bleed" is authoritative for cover math -- the `bleed` parameter is only
    a fallback for a binding with no BINDING_TYPES entry.

    This used to fall through with no binding-specific handling for a
    jacket at all and silently compute plain paperback-wrap dimensions,
    which is exactly what a real IngramSpark rejection called out
    ("SUBMITTED IMAGE IS NOT SETUP CORRECTLY FOR A JACKET COVER" plus a
    bleed-shortfall on the same file) -- the resulting file was ~7" too
    narrow, missing both flaps and their hinges entirely. The case-laminate
    board adjustment (board is narrower and taller than the trim -- boards
    overhang the page block top/bottom, and sit slightly inside it side to
    side) is confirmed exactly against that same rejection's numbers, not
    guessed. Jacket panel width is left at the plain trim size: IngramSpark's
    guide hints at a similar small adjustment there too but the source PDF's
    number for it didn't survive extraction cleanly, so it's left unapplied
    rather than guessed -- verify the exact jacket panel size against
    IngramSpark's Cover Template Generator before a final submission.
    """
    spec = resolve_binding_spec(binding, platform)
    bleed = spec.get("bleed", bleed)
    gutter_hinge = 0.0
    flap_w = 0.0
    wrap_fold = 0.0
    panel_w, panel_h = trim_w, trim_h
    if binding == "hardcover_case":
        gutter_hinge = spec.get("gutter_hinge", 0.0)
        panel_w = trim_w + spec.get("board_width_adjust", 0.0)
        panel_h = trim_h + spec.get("board_height_adjust", 0.0)
    elif binding == "hardcover_jacket":
        flap_w = spec.get("flap", 0.0)
        wrap_fold = spec.get("wrap_fold", 0.0)

    total_w = (bleed * 2) + (gutter_hinge * 2) + (wrap_fold * 2) + (flap_w * 2) + (panel_w * 2) + spine_w
    total_h = panel_h + (bleed * 2)

    back_flap_x = round(bleed, 4) if flap_w else None
    back_x = round(bleed + gutter_hinge + wrap_fold + flap_w, 4)
    spine_x = round(back_x + panel_w, 4)
    front_x = round(spine_x + spine_w, 4)
    front_flap_x = round(front_x + panel_w + wrap_fold, 4) if flap_w else None

    return {
        "total_width": round(total_w, 4),
        "total_height": round(total_h, 4),
        "spine_width": round(spine_w, 4),
        "panel_width": round(panel_w, 4),
        "panel_height": round(panel_h, 4),
        "bleed": bleed,
        "gutter_hinge": gutter_hinge,
        "flap_width": flap_w,
        "wrap_fold": wrap_fold,
        "back_flap_x": back_flap_x,
        "back_x": back_x,
        "spine_x": spine_x,
        "front_x": front_x,
        "front_flap_x": front_flap_x,
    }
