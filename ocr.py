"""OCR helper for OSCN's image-only PDFs.

Pipeline per PDF:
    pdfimages -tiff <pdf> <tmp>/p   ->   <tmp>/p-<N>.tif per page
    tesseract <tiff> stdout -l eng --psm 6   ->   text per page
    join with form-feed (\\f) between pages.

Returns a result dict with text + telemetry. Status classification:
    "ok"          - extraction passed quality thresholds
    "low_quality" - some text but too short / low letter fraction / many � chars
    "empty"       - no images extracted, or all-blank text
    "error"       - pdfimages or tesseract failed (error message captured)
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from pathlib import Path

LETTER_RE = re.compile(r"[A-Za-z]")
REPLACEMENT_CHAR = "�"

DEFAULT_LANGS = "eng"
DEFAULT_PSM = 6  # Assume a single uniform block of text — works well for legal docs

MIN_CHARS_OK = 200
MIN_LETTER_FRAC = 0.4
MAX_REPL_FRAC = 0.05

PDFIMAGES_TIMEOUT_S = 30
TESSERACT_TIMEOUT_S = 60

_TESSERACT_VERSION_CACHE: str | None = None


def tesseract_version() -> str:
    global _TESSERACT_VERSION_CACHE
    if _TESSERACT_VERSION_CACHE is not None:
        return _TESSERACT_VERSION_CACHE
    try:
        out = subprocess.run(
            ["tesseract", "--version"],
            capture_output=True, timeout=5,
        )
        first = (out.stdout or out.stderr).decode("utf-8", errors="ignore").splitlines()
        ver = first[0].strip() if first else "unknown"
    except Exception:
        ver = "unknown"
    _TESSERACT_VERSION_CACHE = ver
    return ver


def classify_text(text: str) -> tuple[str, dict]:
    """Return (status, metrics) given an OCR-extracted text string."""
    n = len(text)
    if n == 0:
        return "empty", {"chars": 0, "letter_frac": 0.0, "repl_frac": 0.0}
    letters = sum(1 for c in text if LETTER_RE.match(c))
    repl_count = text.count(REPLACEMENT_CHAR)
    metrics = {
        "chars": n,
        "letter_frac": round(letters / n, 3),
        "repl_frac": round(repl_count / n, 3),
    }
    if n < MIN_CHARS_OK:
        return "low_quality", metrics
    if metrics["letter_frac"] < MIN_LETTER_FRAC:
        return "low_quality", metrics
    if metrics["repl_frac"] > MAX_REPL_FRAC:
        return "low_quality", metrics
    return "ok", metrics


def ocr_pdf(
    pdf_path: Path,
    *,
    langs: str = DEFAULT_LANGS,
    psm: int = DEFAULT_PSM,
    timeout_s: int = TESSERACT_TIMEOUT_S,
) -> dict:
    """OCR a single PDF. Returns a result dict, never raises.

    Result shape:
        {
          "text": str,
          "status": "ok" | "low_quality" | "empty" | "error",
          "chars": int,
          "pages": int,
          "letter_frac": float,
          "repl_frac": float,
          "elapsed_s": float,
          "engine": str,
          "error": str | None,
        }
    """
    started = time.monotonic()
    result: dict = {
        "text": "",
        "status": "error",
        "chars": 0,
        "pages": 0,
        "letter_frac": 0.0,
        "repl_frac": 0.0,
        "elapsed_s": 0.0,
        "engine": tesseract_version(),
        "error": None,
    }

    with tempfile.TemporaryDirectory(prefix="ocr_") as tmp:
        tmp_path = Path(tmp)

        # Stage 1: pdfimages → TIFFs
        try:
            subprocess.run(
                ["pdfimages", "-tiff", str(pdf_path), str(tmp_path / "p")],
                check=True, capture_output=True, timeout=PDFIMAGES_TIMEOUT_S,
            )
        except subprocess.CalledProcessError as e:
            result["error"] = f"pdfimages: {e.stderr.decode('utf-8', errors='ignore')[:160]}"
            result["elapsed_s"] = round(time.monotonic() - started, 3)
            return result
        except subprocess.TimeoutExpired:
            result["error"] = f"pdfimages timeout (>{PDFIMAGES_TIMEOUT_S}s)"
            result["elapsed_s"] = round(time.monotonic() - started, 3)
            return result

        tiffs = sorted(tmp_path.glob("p-*.tif"))
        result["pages"] = len(tiffs)
        if not tiffs:
            result["status"] = "empty"
            result["error"] = "no images extracted"
            result["elapsed_s"] = round(time.monotonic() - started, 3)
            return result

        # Stage 2: tesseract per page
        text_parts: list[str] = []
        for tiff in tiffs:
            try:
                proc = subprocess.run(
                    ["tesseract", str(tiff), "stdout", "-l", langs, "--psm", str(psm)],
                    check=True, capture_output=True, timeout=timeout_s,
                )
                text_parts.append(proc.stdout.decode("utf-8", errors="replace"))
            except subprocess.CalledProcessError as e:
                result["error"] = (
                    f"tesseract on {tiff.name}: "
                    f"{e.stderr.decode('utf-8', errors='ignore')[:160]}"
                )
                break
            except subprocess.TimeoutExpired:
                result["error"] = f"tesseract timeout on {tiff.name} (>{timeout_s}s)"
                break

        text = "\f".join(text_parts)
        result["text"] = text
        status, metrics = classify_text(text)
        # If we hit a tesseract error mid-doc, keep error status; otherwise use classification
        if result["error"] is None:
            result["status"] = status
        result["chars"] = metrics["chars"]
        result["letter_frac"] = metrics["letter_frac"]
        result["repl_frac"] = metrics["repl_frac"]
        result["elapsed_s"] = round(time.monotonic() - started, 3)
        return result
