#!/usr/bin/env python3
"""
Automate order splitting after Rithum PDF download.

This script watches a folder for new PDFs and automatically runs the existing
splitters (Home Depot / Lowe's / Tractor Supply), then writes:
  - split_report.csv
  - errors_low_confidence.csv
  - per-vendor PDFs
  - optional Print Pack PDF
  - ZIP bundle of generated PDFs
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
from pypdf import PdfReader, PdfWriter

import lowes_core
import split_core
import tsc_core
from vendor_map import load_vendor_map


PRINT_PACK_VENDORS = [
    "Cord Mate",
    "Cornerstone",
    "Gate Latch",
    "Home Selects",
    "Nisus",
    "Post Protector-Here",
    "Soft Seal",
    "Weedshark",
]


@dataclass(frozen=True)
class RetailerConfig:
    code: str
    label: str
    core_module: object
    default_map_candidates: Tuple[Path, ...]


RETAILERS: Dict[str, RetailerConfig] = {
    "hd": RetailerConfig(
        code="hd",
        label="Home Depot",
        core_module=split_core,
        default_map_candidates=(Path("data/vendor_map_hd.xlsx"), Path("data/vendor_map.xlsx")),
    ),
    "lw": RetailerConfig(
        code="lw",
        label="Lowe's",
        core_module=lowes_core,
        default_map_candidates=(Path("data/vendor_map_lowes.xlsx"), Path("data/vendor_map.xlsx")),
    ),
    "tsc": RetailerConfig(
        code="tsc",
        label="Tractor Supply",
        core_module=tsc_core,
        default_map_candidates=(Path("data/vendor_map_tsc.xlsx"), Path("data/vendor_map.xlsx")),
    ),
}


def _norm_vendor(s: str) -> str:
    import re

    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def build_print_pack_alpha(
    out_pdfs: Dict[str, str], out_root: Path, master_name: str
) -> Tuple[Optional[Path], List[str]]:
    targets = {_norm_vendor(v) for v in PRINT_PACK_VENDORS}
    matched: Dict[str, str] = {}
    for vendor_name, pdf_path in out_pdfs.items():
        if _norm_vendor(vendor_name) in targets and Path(pdf_path).exists():
            matched[vendor_name] = pdf_path

    if not matched:
        return None, []

    ordered = sorted(matched.keys(), key=lambda s: s.upper())
    writer = PdfWriter()
    for vendor_name in ordered:
        reader = PdfReader(matched[vendor_name])
        for page in reader.pages:
            writer.add_page(page)

    out_path = out_root / f"{master_name} - Print Pack.pdf"
    with open(out_path, "wb") as f:
        writer.write(f)
    return out_path, ordered


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch a folder and auto-process new order PDFs."
    )
    parser.add_argument(
        "--retailer",
        required=True,
        choices=sorted(RETAILERS.keys()),
        help="Retailer mode: hd, lw, or tsc.",
    )
    parser.add_argument(
        "--watch-dir",
        default="~/Downloads",
        help="Folder where new PDFs land (default: ~/Downloads).",
    )
    parser.add_argument(
        "--output-dir",
        default="output/auto",
        help="Root output folder for automation runs.",
    )
    parser.add_argument(
        "--map-path",
        default=None,
        help="Optional explicit vendor map XLSX path. If omitted, default map is used.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.88,
        help="Auto-routing confidence threshold (default: 0.88).",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="How often to scan for new PDFs in watch mode (default: 5).",
    )
    parser.add_argument(
        "--min-file-age-seconds",
        type=float,
        default=5.0,
        help="Ignore files newer than this age to avoid in-progress downloads (default: 5).",
    )
    parser.add_argument(
        "--include-existing",
        action="store_true",
        help="Process PDFs already in watch-dir at startup.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Scan one time and exit (no continuous watch).",
    )
    parser.add_argument(
        "--archive-dir",
        default=None,
        help="Optional folder to move processed source PDFs into.",
    )
    return parser.parse_args()


def resolve_map_path(config: RetailerConfig, explicit: Optional[str]) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"Map path does not exist: {p}")
        return p
    for candidate in config.default_map_candidates:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError(
        f"No default map found for {config.label}. "
        "Set one in data/ or pass --map-path."
    )


def pdf_fingerprint(path: Path) -> str:
    st = path.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def discover_pdfs(watch_dir: Path) -> Iterable[Path]:
    return sorted(
        [p for p in watch_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"],
        key=lambda p: p.stat().st_mtime_ns,
    )


def read_index(index_path: Path) -> Dict[str, str]:
    if not index_path.exists():
        return {}
    try:
        return json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_index(index_path: Path, idx: Dict[str, str]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(idx, indent=2, sort_keys=True), encoding="utf-8")


def safe_move_to_archive(src: Path, archive_dir: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / src.name
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = archive_dir / f"{src.stem}_{stamp}{src.suffix}"
    shutil.move(str(src), str(target))
    return target


def process_pdf(
    pdf_path: Path,
    config: RetailerConfig,
    map_path: Path,
    output_dir: Path,
    threshold: float,
) -> Path:
    now = datetime.now()
    date_dir = now.strftime("%Y-%m-%d")
    run_stamp = now.strftime("%H%M%S")
    master_name = pdf_path.stem
    run_dir = output_dir / config.code.upper() / date_dir / f"{master_name}_{run_stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    input_copy = run_dir / "input.pdf"
    shutil.copy2(pdf_path, input_copy)
    local_map = run_dir / "vendor_map.xlsx"
    shutil.copy2(map_path, local_map)

    vendor_map = load_vendor_map(str(local_map))
    os.environ["HD_MASTER_NAME"] = master_name
    report_rows, review_rows, out_pdfs = config.core_module.split_pdf_to_vendors(
        str(input_copy), str(run_dir), vendor_map, threshold=threshold
    )

    report_df = pd.DataFrame(report_rows)
    review_df = pd.DataFrame(review_rows)
    report_path = run_dir / "split_report.csv"
    review_path = run_dir / "errors_low_confidence.csv"
    report_df.to_csv(report_path, index=False)
    review_df.to_csv(review_path, index=False)

    print_pack_path, included = build_print_pack_alpha(out_pdfs, run_dir, master_name)

    zip_name = f"{master_name} - vendor_pdfs.zip"
    zip_path = run_dir / zip_name
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for _, vendor_pdf in sorted(out_pdfs.items(), key=lambda kv: kv[0].upper()):
            zf.write(vendor_pdf, arcname=Path(vendor_pdf).name)
        if print_pack_path and print_pack_path.exists():
            zf.write(print_pack_path, arcname=print_pack_path.name)
    zip_path.write_bytes(zip_buf.getvalue())

    summary = {
        "source_pdf": str(pdf_path),
        "retailer": config.label,
        "output_dir": str(run_dir),
        "map_used": str(map_path),
        "threshold": threshold,
        "created_at": now.isoformat(),
        "pages_total": len(report_df),
        "pages_needing_review": len(review_df),
        "vendors_generated": sorted(out_pdfs.keys()),
        "print_pack_included": included,
        "zip_file": str(zip_path),
    }
    (run_dir / "run_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    return run_dir


def main() -> int:
    args = parse_args()
    config = RETAILERS[args.retailer]
    watch_dir = Path(args.watch_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    archive_dir = (
        Path(args.archive_dir).expanduser().resolve() if args.archive_dir else None
    )

    if not watch_dir.exists() or not watch_dir.is_dir():
        print(f"[ERROR] watch-dir is not a valid directory: {watch_dir}", file=sys.stderr)
        return 2

    try:
        map_path = resolve_map_path(config, args.map_path)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    retailer_root = output_dir / config.code.upper()
    index_path = retailer_root / ".processed_pdfs.json"
    processed = read_index(index_path)

    if not args.include_existing:
        seeded = 0
        for pdf in discover_pdfs(watch_dir):
            try:
                key = str(pdf.resolve())
                if key not in processed:
                    # Seed current files to avoid immediate back-processing.
                    processed[key] = pdf_fingerprint(pdf)
                    seeded += 1
            except FileNotFoundError:
                continue
        if seeded:
            write_index(index_path, processed)
            print(f"[INFO] Seeded {seeded} existing PDFs as already seen.")

    print(f"[INFO] Retailer: {config.label}")
    print(f"[INFO] Watching: {watch_dir}")
    print(f"[INFO] Output root: {output_dir}")
    print(f"[INFO] Vendor map: {map_path}")
    print(
        "[INFO] Mode: one-shot" if args.once else f"[INFO] Mode: continuous ({args.poll_seconds}s poll)"
    )

    def _eligible(pdf: Path) -> bool:
        try:
            age = time.time() - pdf.stat().st_mtime
            return age >= args.min_file_age_seconds
        except FileNotFoundError:
            return False

    while True:
        processed_any = False
        for pdf in discover_pdfs(watch_dir):
            key = str(pdf.resolve())
            if not _eligible(pdf):
                continue
            try:
                fp = pdf_fingerprint(pdf)
            except FileNotFoundError:
                continue
            if processed.get(key) == fp:
                continue

            print(f"[INFO] Processing {pdf.name} ...")
            try:
                run_dir = process_pdf(
                    pdf_path=pdf,
                    config=config,
                    map_path=map_path,
                    output_dir=output_dir,
                    threshold=args.threshold,
                )
                print(f"[OK] Completed {pdf.name} -> {run_dir}")
                if archive_dir:
                    moved = safe_move_to_archive(pdf, archive_dir)
                    print(f"[INFO] Archived source PDF -> {moved}")
                processed[key] = fp
                write_index(index_path, processed)
                processed_any = True
            except Exception as exc:
                print(f"[ERROR] Failed {pdf.name}: {exc}", file=sys.stderr)

        if args.once:
            break

        if not processed_any:
            time.sleep(args.poll_seconds)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
