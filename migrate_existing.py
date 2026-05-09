"""One-shot migration: data/CJ_YYYY_N/ -> data/YYYY-MM-DD/<CASE>/ + OCR every PDF.

Per source dir:
  1. Read register_of_actions.json. Get filing_date (or fall back to first
     action's MM-DD-YYYY date, or metadata.filed in MM/DD/YYYY).
  2. Compute target dir: data/<filing_iso>/<case_number>/.
  3. Move source dir to target. Skip if target already exists; the OCR
     pass below picks up any PDFs left there.
  4. OCR each .pdf in the moved dir using ocr.ocr_pdf().
     - On status="ok": write .txt next to the .pdf, delete the .pdf.
     - On any other status: keep the .pdf for a future OCR re-pass with
       different settings (e.g., --psm 1 or cloud OCR).
  5. Patch register_of_actions.json with per-action text_* fields, set
     metadata.migrated_from_v1=True, refresh case_number / filing_date.

Idempotent: re-running picks up where it left off (skips moves that
already happened, skips OCRs where .txt already exists).

Usage:
    python ok_scraper/migrate_existing.py            # do it
    python ok_scraper/migrate_existing.py --dry-run  # show plan only
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

# Allow `import ocr` when running as a script from the repo root or this dir.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ocr import ocr_pdf  # noqa: E402

DATA_ROOT = Path(__file__).resolve().parent / "data"
V1_PREFIXES = ("CJ_", "CV_", "CF_", "CM_", "PO_", "DM_", "MI_", "PB_", "TR_", "YO_", "SC_")


def iso_from_register(register: dict) -> str | None:
    """Best-effort filing-date ISO string from a v1 register."""
    metadata = register.get("metadata", {})

    iso = metadata.get("filing_date")
    if isinstance(iso, str) and len(iso) == 10 and iso[4] == "-":
        return iso

    # First action's date is "MM-DD-YYYY" in v1
    actions = register.get("actions") or []
    for a in actions:
        d = a.get("date")
        if isinstance(d, str) and len(d) == 10 and d[2] == "-":
            try:
                m, dd, y = d.split("-")
                return f"{y}-{m}-{dd}"
            except ValueError:
                continue

    # metadata.filed is "MM/DD/YYYY"
    filed = metadata.get("filed")
    if isinstance(filed, str) and "/" in filed:
        try:
            m, dd, y = filed.split("/")
            return f"{y.zfill(4)}-{m.zfill(2)}-{dd.zfill(2)}"
        except ValueError:
            pass

    return None


def derive_case_number(src_dir_name: str, register: dict) -> str:
    """Best-effort case number for the migrated directory's name."""
    cn = register.get("metadata", {}).get("case_number")
    if isinstance(cn, str) and "-" in cn:
        return cn
    # Fall back: convert "CJ_2024_1234" -> "CJ-2024-1234"
    return src_dir_name.replace("_", "-")


def find_action_for_pdf(register: dict, pdf_name: str) -> dict | None:
    for a in register.get("actions", []):
        if a.get("doc_filename") == pdf_name:
            return a
    return None


def migrate_one(src_dir: Path, dry_run: bool) -> dict:
    stats = {
        "src": src_dir.name,
        "target": None,
        "moved": False,
        "merged": False,
        "pdfs_seen": 0,
        "ocr_ok": 0,
        "ocr_failed": 0,
        "ocr_skipped_existing": 0,
        "warning": None,
    }

    register_path = src_dir / "register_of_actions.json"
    if not register_path.exists():
        stats["warning"] = "no register_of_actions.json"
        return stats

    try:
        register = json.loads(register_path.read_text())
    except Exception as e:
        stats["warning"] = f"register parse error: {e}"
        return stats

    iso = iso_from_register(register)
    if not iso:
        stats["warning"] = "no usable filing_date"
        return stats

    case_number = derive_case_number(src_dir.name, register)
    target_dir = DATA_ROOT / iso / case_number
    stats["target"] = str(target_dir.relative_to(DATA_ROOT))
    stats["pdfs_seen"] = len(list(src_dir.glob("*.pdf")))

    if dry_run:
        if target_dir.exists() and target_dir != src_dir:
            stats["warning"] = f"would merge into existing target {stats['target']}"
        return stats

    target_existed = target_dir.exists() and target_dir != src_dir

    if target_existed:
        # Merge: move PDFs from source into the existing target, then drop
        # the source dir (its register may be older/sparser than the target).
        target_dir.mkdir(parents=True, exist_ok=True)
        for pdf in src_dir.glob("*.pdf"):
            dest = target_dir / pdf.name
            if not dest.exists():
                shutil.move(str(pdf), str(dest))
        # Prefer the target's existing register; if missing/empty, copy ours.
        target_register_path = target_dir / "register_of_actions.json"
        if not target_register_path.exists():
            shutil.copy2(register_path, target_register_path)
        stats["merged"] = True
        # Best-effort cleanup of the source dir (only if now empty besides
        # files we don't care about).
        try:
            for leftover in src_dir.iterdir():
                if leftover.is_file():
                    leftover.unlink()
            src_dir.rmdir()
        except OSError:
            pass
        # Reload from the merged target for the OCR pass.
        try:
            register = json.loads(target_register_path.read_text())
        except Exception as e:
            stats["warning"] = f"merged but target register unreadable: {e}"
            return stats
        register_path = target_register_path
    elif src_dir != target_dir:
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_dir), str(target_dir))
        stats["moved"] = True

    # OCR every PDF in the moved/merged dir
    pdfs = sorted(target_dir.glob("*.pdf"))
    stats["pdfs_seen"] = len(pdfs)

    for pdf in pdfs:
        action = find_action_for_pdf(register, pdf.name)
        if action is None:
            # Pre-bc-naming PDFs (description-based filenames) lack an
            # action record; synthesize a minimal one so we still
            # capture the OCR telemetry.
            action = {
                "doc_filename": pdf.name,
                "proceedings": pdf.stem.replace("_", " "),
                "date": "",
                "doc_url": None,
                "doc_id": "",
                "synthesized": True,
            }
            register.setdefault("actions", []).append(action)

        txt_path = pdf.with_suffix(".txt")
        if txt_path.exists() and txt_path.stat().st_size > 0:
            # Already OCR'd in a prior run; just record it on the action.
            action["text_filename"] = txt_path.name
            stats["ocr_skipped_existing"] += 1
            continue

        result = ocr_pdf(pdf)
        action["text_chars"] = result["chars"]
        action["text_pages"] = result["pages"]
        action["text_letter_frac"] = result["letter_frac"]
        action["text_extraction_status"] = result["status"]
        action["text_extraction_elapsed_s"] = result["elapsed_s"]
        action["ocr_engine"] = result["engine"]
        if result.get("error"):
            action["text_extraction_error"] = result["error"][:200]

        if result["status"] == "ok":
            txt_path.write_text(result["text"])
            action["text_filename"] = txt_path.name
            pdf.unlink()
            action["doc_filename"] = None
            stats["ocr_ok"] += 1
        else:
            # Save whatever text we got (low_quality may still be useful)
            if result["text"]:
                txt_path.write_text(result["text"])
                action["text_filename"] = txt_path.name
            stats["ocr_failed"] += 1

    # Update metadata in place
    md = register.setdefault("metadata", {})
    md["migrated_from_v1"] = True
    md["case_number"] = case_number
    md["filing_date"] = iso
    timing = md.setdefault("timing", {})
    timing["ocr_docs_ok"] = stats["ocr_ok"]
    timing["ocr_docs_failed"] = stats["ocr_failed"]

    (target_dir / "register_of_actions.json").write_text(json.dumps(register, indent=2))
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't move or OCR; just report what would happen.")
    args = parser.parse_args()

    src_dirs = sorted(
        p for p in DATA_ROOT.iterdir()
        if p.is_dir() and any(p.name.startswith(prefix) for prefix in V1_PREFIXES)
    )
    print(f"Found {len(src_dirs)} v1 case dirs under {DATA_ROOT}")
    if not src_dirs:
        return

    started = time.monotonic()
    agg = {"moved": 0, "ocr_ok": 0, "ocr_failed": 0, "ocr_skipped_existing": 0,
           "warnings": 0, "no_pdfs": 0}

    for i, src in enumerate(src_dirs, 1):
        stats = migrate_one(src, dry_run=args.dry_run)
        prefix = f"[{i:>3}/{len(src_dirs)}]"
        if stats["warning"]:
            agg["warnings"] += 1
            print(f"{prefix} {src.name}: skip ({stats['warning']})")
            continue
        if stats["moved"]:
            agg["moved"] += 1
        if stats["pdfs_seen"]:
            agg["ocr_ok"] += stats["ocr_ok"]
            agg["ocr_failed"] += stats["ocr_failed"]
            agg["ocr_skipped_existing"] += stats["ocr_skipped_existing"]
            print(
                f"{prefix} {src.name} -> {stats['target']}  "
                f"ocr_ok={stats['ocr_ok']} failed={stats['ocr_failed']} "
                f"cached={stats['ocr_skipped_existing']}"
            )
        else:
            agg["no_pdfs"] += 1
            print(f"{prefix} {src.name} -> {stats['target']}  (no PDFs)")

    elapsed = time.monotonic() - started
    print()
    print("=== Summary ===")
    print(f"Cases moved:                {agg['moved']}")
    print(f"PDFs OCR'd successfully:    {agg['ocr_ok']}")
    print(f"PDFs OCR'd low-quality/err: {agg['ocr_failed']}")
    print(f"PDFs already had .txt:      {agg['ocr_skipped_existing']}")
    print(f"Cases with no PDFs:         {agg['no_pdfs']}")
    print(f"Warnings/skipped:           {agg['warnings']}")
    print(f"Elapsed:                    {elapsed:.1f}s")


if __name__ == "__main__":
    main()
