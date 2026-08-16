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
import shutil
import subprocess
from pathlib import Path
from typing import Optional

GS_BINARY_CANDIDATES = ["gs", "gswin64c", "gswin32c"]


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
) -> dict:
    """Authoritative PDF/X-1a:2001 conversion via Ghostscript's built-in
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
        "-sColorConversionStrategy=CMYK",
        "-dProcessColorModel=/DeviceCMYK",
        "-dPreserveTransparency=false",
        f"-sOutputFile={output_path}",
    ]
    if icc_profile_path:
        cmd.insert(-1, f"-sOutputICCProfile={icc_profile_path}")
    cmd.append(input_path)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"Ghostscript PDF/X-1a conversion failed: {result.stderr[-2000:]}")

    return {
        "output_path": output_path,
        "pdf_standard": "PDF/X-1a:2001",
        "converter": "ghostscript",
        "icc_profile_used": icc_profile_path or "ghostscript default CMYK",
        "gs_stderr_tail": result.stderr[-500:] if result.stderr else "",
    }
