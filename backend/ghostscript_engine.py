"""Ghostscript integration: transparency flattening and authoritative
PDF/X-1a conversion/validation via the actual Ghostscript binary.

pikepdf (in pdfx_validator.py and file_processor.py) can *detect* most
compliance problems and *set* the PDF/X-1a metadata flags, but it can't
rasterize/flatten live transparency the way a real RIP would -- that
requires Ghostscript's rendering engine. This module shells out to the
`gs` binary the same way file_processor.py shells out to nothing (it's
pure Python) and OCRExtractor shells out to `tesseract`: an external
program that must be installed separately from pip packages.

INSTALL: Ghostscript is not a Python package. It must be installed on
whatever machine/container runs this code:
    Debian/Ubuntu:  apt-get install -y ghostscript
    macOS:           brew install ghostscript
    Windows:         https://ghostscript.com/releases/gsdnld.html
Same category of dependency as Tesseract OCR -- add it to the Docker
image's apt-get line alongside tesseract-ocr.
"""
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

GS_BINARY_CANDIDATES = ["gs", "gswin64c", "gswin32c"]

# The 14 standard PDF fonts (and their usual aliases). Ghostscript ships
# metric-compatible URW copies of these (Nimbus Sans for Helvetica, Nimbus
# Roman for Times, ...), so when a file names one of them without embedding
# it, Ghostscript embeds a font with the exact same character widths --
# text keeps every line break. Any OTHER font Ghostscript can't find gets a
# fallback with different widths, which can move text, so those are
# reported back rather than silently counted as fixed.
_BASE14_PREFIXES = (
    "Helvetica", "Arial", "Times", "Courier", "Symbol", "ZapfDingbats", "Dingbats",
)
_LOADING_FONT_RE = re.compile(r"Loading font (\S+) \(or substitute\) from (\S+)")
_SUBST_RE = re.compile(r"Substituting font (\S+) for (\S+)")


def _strip_subset_tag(name: str) -> str:
    """'ABCDEF+Garamond' -> 'Garamond' (a subset tag is 6 capitals and a plus)."""
    return name.split("+", 1)[1] if re.match(r"^[A-Z]{6}\+", name) else name


def font_substitutions(gs_output: str) -> List[dict]:
    """Fonts Ghostscript had to stand in for with a DIFFERENT font while
    embedding (not one of the 14 standard fonts, which have exact-width
    copies). Read from Ghostscript's own console output."""
    found = {}
    for m in _LOADING_FONT_RE.finditer(gs_output or ""):
        requested, source = _strip_subset_tag(m.group(1)), m.group(2)
        base = Path(source).name
        if requested.lstrip("/").startswith(_BASE14_PREFIXES):
            continue
        # Ghostscript found the real font file (its own file name matches the name asked for).
        if requested.lower().replace("-", "") in base.lower().replace("-", ""):
            continue
        found[requested] = base
    for m in _SUBST_RE.finditer(gs_output or ""):
        substitute, requested = m.group(1).lstrip("/"), _strip_subset_tag(m.group(2).lstrip("/"))
        if not requested.startswith(_BASE14_PREFIXES):
            found.setdefault(requested, substitute)
    return [{"font": k, "substitute": v} for k, v in sorted(found.items())]


def find_ghostscript() -> Optional[str]:
    """Returns the path to the Ghostscript binary if installed, else None.
    Callers must check this before calling the functions below --
    they raise RuntimeError with a clear message if gs isn't found,
    rather than failing with a confusing 'file not found' from subprocess.
    """
    for candidate in GS_BINARY_CANDIDATES:
        path = shutil.which(candidate)
        if path:
            return path
    return None


def _require_gs() -> str:
    gs = find_ghostscript()
    if not gs:
        raise RuntimeError(
            "Ghostscript is not installed on this machine. Install it with "
            "'apt-get install -y ghostscript' (Linux) or see "
            "https://ghostscript.com/releases/gsdnld.html -- this is a separate "
            "program from any pip package, same as Tesseract OCR."
        )
    return gs


def flatten_transparency(input_path: str, output_path: str, dpi: int = 300) -> dict:
    """Rasterizes/flattens all live transparency in a PDF by round-tripping
    it through Ghostscript's pdfwrite device. This is the real fix for
    the 'live_transparency_detected' finding from pdfx_validator.py --
    pikepdf can detect transparency but can't flatten it; Ghostscript's
    rendering pipeline is what actually does the flattening.
    """
    gs = _require_gs()
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        gs, "-dBATCH", "-dNOPAUSE", "-dSAFER", "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        f"-r{dpi}",
        "-dPreserveTransparency=false",  # forces flattening rather than preserving groups
        f"-sOutputFile={output_path}",
        input_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"Ghostscript flatten failed: {result.stderr[-2000:]}")
    return {
        "output_path": output_path,
        "dpi": dpi,
        "flattened": True,
        "gs_stderr_tail": result.stderr[-500:] if result.stderr else "",
    }


def convert_to_pdfx1a(
    input_path: str,
    output_path: str,
    icc_profile_path: Optional[str] = None,
    title: str = "SparkPrep Export",
    timeout_s: int = 300,
    grayscale: bool = False,
) -> dict:
    """grayscale=True (black & white books): every colour becomes black ink only, so no page can go over 100%
    total ink -- screen-black text otherwise converts to ~294% "rich black" and fails the printer's limit.

    Authoritative PDF/X-1a:2001 conversion via Ghostscript's built-in
    PDF/X pipeline (-dPDFX). This is the industry-standard way to produce
    PDF/X-1a and is what most print distributors' own preflight tools
    use internally, so a file that passes gs's -dPDFX conversion is
    about as reliable a compliance signal as exists outside a paid
    preflight product (e.g. Enfocus PitStop).

    Requires an ICC output profile for full PDF/X-1a compliance -- pass
    icc_profile_path if you have one (e.g. a bundled SWOP profile), or
    Ghostscript will use its built-in default CMYK profile.
    """
    gs = _require_gs()
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        gs, "-dBATCH", "-dNOPAUSE", "-dSAFER",
        "-sDEVICE=pdfwrite",
        "-dPDFX", "-dPDFSETTINGS=/prepress",
        "-dCompatibilityLevel=1.4",
        "-sColorConversionStrategy=Gray" if grayscale else "-sColorConversionStrategy=CMYK",
        "-dProcessColorModel=/DeviceGray" if grayscale else "-dProcessColorModel=/DeviceCMYK",
        "-dPreserveTransparency=false",
        f"-sOutputFile={output_path}",
    ]
    if icc_profile_path:
        cmd.insert(-1, f"-sOutputICCProfile={icc_profile_path}")
    cmd.append(input_path)

    # -dEmbedAllFonts/-dSubsetFonts are already on under /prepress; spelled
    # out here because embedding every font is one of the things Auto-Fix
    # promises, so it shouldn't depend on a preset's defaults.
    cmd.insert(-1, "-dEmbedAllFonts=true")
    cmd.insert(-1, "-dSubsetFonts=true")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    if result.returncode != 0:
        raise RuntimeError(f"Ghostscript PDF/X-1a conversion failed: {result.stderr[-2000:]}")

    return {
        "font_substitutions": font_substitutions((result.stdout or "") + "\n" + (result.stderr or "")),
        "output_path": output_path,
        "pdf_standard": "PDF/X-1a:2001",
        "converter": "ghostscript",
        "icc_profile_used": icc_profile_path or "ghostscript default CMYK",
        "gs_stderr_tail": result.stderr[-500:] if result.stderr else "",
    }
