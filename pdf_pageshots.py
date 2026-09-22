#!/usr/bin/env python3
"""Convert PDF pages to page-wise PNG/JPEG images using PyMuPDF's new API."""

from __future__ import annotations

import argparse
import math
import shutil
import sys
import time
from pathlib import Path

import pymupdf  # PyMuPDF


RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
RED = "\033[31m"


def fmt_bytes(value: int | float) -> str:
	value = float(value)
	for unit in ("B", "KB", "MB", "GB"):
		if value < 1024 or unit == "GB":
			return f"{value:.1f} {unit}"
		value /= 1024
	return f"{value:.1f} GB"


def fmt_ms(ms: float) -> str:
	return f"{ms:.0f} ms"


def log_line(message: str, *, verbose: bool, state: dict[str, int]) -> None:
	if verbose:
		print(message, flush=True)
		return

	prev_len = state.get("line_len", 0)
	padding = max(0, prev_len - len(message))
	print(f"\r{message}{' ' * padding}", end="", flush=True)
	state["line_len"] = len(message)


def finish_log_line(verbose: bool, state: dict[str, int]) -> None:
	if not verbose:
		print()
		state["line_len"] = 0


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Convert PDFs to page-wise PNG/JPEG images using PyMuPDF."
	)
	parser.add_argument("-i", "--input", required=True, type=Path, help="Input directory containing PDFs.")
	parser.add_argument("-o", "--output", required=True, type=Path, help="Output directory; must not exist or contain anything.")
	parser.add_argument("-r", "--recursive", action="store_true", help="Process PDFs in all input subdirectories.")
	parser.add_argument("--jpeg", "--jpg", dest="jpeg", action="store_true", help="Save images as JPEG/JPG.")
	parser.add_argument("-W", "--width", type=int, help="Target image width in pixels.")
	parser.add_argument("-H", "--height", type=int, help="Target image height in pixels.")
	parser.add_argument("--stack", action="store_true", help="Stack all pages into one image.")
	parser.add_argument("-n", "--limit", type=int, help="Process only the first N PDFs.")
	parser.add_argument("-p", "--pages", type=int, help="Process only the first P pages of each PDF.")
	parser.add_argument("--verbose", action="store_true", help="Print every image-save log on its own line.")
	return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
	if not args.input.is_dir():
		raise SystemExit(f"{RED}❌ Input directory does not exist: {args.input}{RESET}")

	if args.output.exists():
		try:
			nonempty = any(args.output.iterdir())
		except OSError as exc:
			raise SystemExit(f"{RED}❌ Cannot inspect output directory: {exc}{RESET}") from exc
		if nonempty:
			raise SystemExit(f"{RED}❌ Output must not exist or contain anything: {args.output}{RESET}")
		# Existing empty output dirs are rejected too, per the CLI contract.
		raise SystemExit(f"{RED}❌ Output directory already exists: {args.output}{RESET}")

	if args.width is not None and args.width <= 0:
		raise SystemExit(f"{RED}❌ Width must be > 0.{RESET}")
	if args.height is not None and args.height <= 0:
		raise SystemExit(f"{RED}❌ Height must be > 0.{RESET}")
	if args.limit is not None and args.limit <= 0:
		raise SystemExit(f"{RED}❌ Limit must be > 0.{RESET}")
	if args.pages is not None and args.pages <= 0:
		raise SystemExit(f"{RED}❌ Pages must be > 0.{RESET}")

	if args.stack and args.width is None and args.height is None:
		raise SystemExit(f"{RED}❌ --stack requires --width and/or --height so output dimensions are defined.{RESET}")


def find_pdfs(input_dir: Path, recursive: bool) -> list[Path]:
	pattern = "**/*.pdf" if recursive else "*.pdf"
	return sorted((p for p in input_dir.glob(pattern) if p.is_file()), key=lambda p: p.as_posix().lower())


def make_matrix(page: pymupdf.Page, width: int | None, height: int | None) -> pymupdf.Matrix:
	rect = page.rect
	if width is None and height is None:
		return pymupdf.Matrix(1, 1)

	scale_x = width / rect.width if width is not None else None
	scale_y = height / rect.height if height is not None else None

	if scale_x is None:
		scale_x = scale_y
	if scale_y is None:
		scale_y = scale_x

	# pymupdf.Matrix supports independent x/y scaling, preserving aspect ratio when
	# only one dimension is requested and using exact dimensions when both are set.
	return pymupdf.Matrix(scale_x, scale_y)


def pixmap_for_page(page: pymupdf.Page, width: int | None, height: int | None) -> pymupdf.Pixmap:
	matrix = make_matrix(page, width, height)
	return page.get_pixmap(matrix=matrix, alpha=False)


def image_ext(jpeg: bool) -> str:
	return "jpg" if jpeg else "png"


def save_pixmap(pix: pymupdf.Pixmap, path: Path, jpeg: bool) -> int:
	if jpeg:
		pix.save(path.as_posix(), jpg_quality=95)
	else:
		pix.save(path.as_posix())
	return path.stat().st_size


def stack_pixmaps(pixmaps: list[pymupdf.Pixmap], horizontal: bool) -> pymupdf.Pixmap:
	if not pixmaps:
		raise ValueError("No pages to stack.")

	if horizontal:
		total_width = sum(p.width for p in pixmaps)
		max_height = max(p.height for p in pixmaps)
		out = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, total_width, max_height))
		out.clear_with(255)
		x = 0
		for pix in pixmaps:
			out.copy(pix, pymupdf.IRect(x, 0, x + pix.width, pix.height))
			x += pix.width
		return out

	max_width = max(p.width for p in pixmaps)
	total_height = sum(p.height for p in pixmaps)
	out = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, max_width, total_height))
	out.clear_with(255)
	y = 0
	for pix in pixmaps:
		out.copy(pix, pymupdf.IRect(0, y, pix.width, y + pix.height))
		y += pix.height
	return out


def process_pdf(
	pdf_path: Path,
	input_dir: Path,
	output_dir: Path,
	args: argparse.Namespace,
	state: dict[str, int],
) -> tuple[int, int, float]:
	pdf_start = time.perf_counter()

	relative = pdf_path.relative_to(input_dir)
	pdf_out_dir = output_dir / relative.with_suffix("")
	pdf_out_dir.mkdir(parents=True, exist_ok=True)

	doc = pymupdf.open(pdf_path)
	try:
		page_count = min(len(doc), args.pages or len(doc))
		if page_count == 0:
			return 0, 0, (time.perf_counter() - pdf_start) * 1000

		pixmaps: list[pymupdf.Pixmap] = []
		total_bytes = 0
		ext = image_ext(args.jpeg)

		for page_index in range(page_count):
			page = doc.load_page(page_index)
			pix = pixmap_for_page(page, args.width, args.height)

			if args.stack:
				pixmaps.append(pix)
				continue

			filename = f"{pdf_path.stem}-page-{page_index + 1:03d}.{ext}"
			out_path = pdf_out_dir / filename

			start = time.perf_counter()
			size = save_pixmap(pix, out_path, args.jpeg)
			elapsed_ms = (time.perf_counter() - start) * 1000
			total_bytes += size

			log_line(
				f"  {GREEN}💾{RESET} {out_path.relative_to(output_dir)} "
				f"{DIM}[{pix.width}×{pix.height}, {fmt_bytes(size)}, {fmt_ms(elapsed_ms)}]{RESET}",
				verbose=args.verbose,
				state=state,
			)

		if args.stack:
			# Horizontal only when height is explicitly set and width is not.
			horizontal = args.height is not None and args.width is None
			start = time.perf_counter()
			stacked = stack_pixmaps(pixmaps, horizontal=horizontal)
			filename = f"{pdf_path.stem}-stacked.{ext}"
			out_path = pdf_out_dir / filename
			size = save_pixmap(stacked, out_path, args.jpeg)
			elapsed_ms = (time.perf_counter() - start) * 1000
			total_bytes = size

			log_line(
				f"  {MAGENTA}🧩{RESET} {out_path.relative_to(output_dir)} "
				f"{DIM}[{stacked.width}×{stacked.height}, {fmt_bytes(size)}, {fmt_ms(elapsed_ms)}]{RESET}",
				verbose=args.verbose,
				state=state,
			)

			for pix in pixmaps:
				pix = None

		return page_count, total_bytes, (time.perf_counter() - pdf_start) * 1000
	finally:
		doc.close()


def main() -> int:
	args = parse_args()
	validate_args(args)

	pdfs = find_pdfs(args.input, args.recursive)
	if args.limit is not None:
		pdfs = pdfs[:args.limit]

	if not pdfs:
		print(f"{YELLOW}⚠️  No PDFs found in {args.input}{RESET}")
		return 0

	args.output.mkdir(parents=True)

	print(f"{BOLD}{CYAN}📸 PDF PageShots{RESET}")
	print(f"   📥 Input : {args.input}")
	print(f"   📤 Output: {args.output}")
	print(f"   📚 PDFs  : {len(pdfs)}")
	print(f"   🖼️  Format: {'JPEG' if args.jpeg else 'PNG'}")
	if args.width or args.height:
		print(f"   📐 Size  : width={args.width or 'auto'}, height={args.height or 'auto'}")
	if args.stack:
		orientation = "horizontal" if args.height is not None and args.width is None else "vertical"
		print(f"   🧩 Stack : {orientation}")
	print()

	state: dict[str, int] = {}
	total_start = time.perf_counter()
	total_images = 0
	total_bytes = 0
	total_pdf_ms = 0.0

	for index, pdf_path in enumerate(pdfs, start=1):
		log_line(
			f"{BOLD}{CYAN}📄 [{index}/{len(pdfs)}] {pdf_path.relative_to(args.input)}{RESET}",
			verbose=args.verbose,
			state=state,
		)
		if not args.verbose:
			finish_log_line(args.verbose, state)

		try:
			image_count, byte_count, pdf_ms = process_pdf(
				pdf_path, args.input, args.output, args, state
			)
			total_images += image_count
			total_bytes += byte_count
			total_pdf_ms += pdf_ms

			avg_size = byte_count / image_count if image_count else 0
			finish_log_line(args.verbose, state)
			print(
				f"  {GREEN}✅ Done{RESET} "
				f"{DIM}pages={image_count}, total={fmt_bytes(byte_count)}, "
				f"avg={fmt_bytes(avg_size)}, time={fmt_ms(pdf_ms)}{RESET}"
			)
		except Exception as exc:
			finish_log_line(args.verbose, state)
			print(f"  {RED}❌ Failed{RESET}: {pdf_path}: {exc}", file=sys.stderr)

	elapsed_ms = (time.perf_counter() - total_start) * 1000
	avg_image_size = total_bytes / total_images if total_images else 0

	print()
	print(f"{BOLD}{GREEN}🎉 All done!{RESET}")
	print(
		f"   📚 PDFs processed : {len(pdfs)}\n"
		f"   🖼️  Images         : {total_images}\n"
		f"   💾 Total size     : {fmt_bytes(total_bytes)}\n"
		f"   📊 Average image  : {fmt_bytes(avg_image_size)}\n"
		f"   ⏱️  Total time     : {fmt_ms(elapsed_ms)}"
	)

	return 0


if __name__ == "__main__":
	raise SystemExit(main())
