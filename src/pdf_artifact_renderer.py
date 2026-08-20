"""Local, deterministic PDF rendering for private application artifacts."""

from __future__ import annotations

import hashlib
from html import escape
import os
from pathlib import Path
import re
import shutil
import tempfile
import unicodedata

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from reportlab.pdfgen.canvas import Canvas

from application_artifacts import RenderedArtifactBundle


_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}")


class LocalPdfArtifactRenderer:
    """Render private PDFs, then publish one immutable, hash-checked bundle."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._staging = self._root / ".staging"
        _private_directory(self._root)
        _private_directory(self._staging)

    def render(
        self,
        *,
        application_id: str,
        bundle_version: str,
        cv_text: str,
        cover_letter_text: str,
    ) -> RenderedArtifactBundle:
        application = _safe_component(application_id)
        generation = _safe_component(bundle_version)
        destination = self._staging / application / generation
        _private_directory(destination)
        cv = destination / "cv.pdf"
        cover_letter = destination / "cover-letter.pdf"
        _write_pdf(cv, cv_text, max_pages=2)
        _write_pdf(cover_letter, cover_letter_text, max_pages=2)
        return RenderedArtifactBundle(
            cv_path=str(cv),
            cover_letter_path=str(cover_letter),
            cv_hash=_file_hash(cv),
            cover_letter_hash=_file_hash(cover_letter),
        )

    def publish(
        self,
        *,
        application_id: str,
        bundle_version: str,
        rendered: RenderedArtifactBundle,
    ) -> RenderedArtifactBundle:
        application = _safe_component(application_id)
        version = _safe_component(bundle_version)
        cv_source = self._trusted_staged_file(rendered.cv_path, rendered.cv_hash)
        cover_source = self._trusted_staged_file(
            rendered.cover_letter_path, rendered.cover_letter_hash
        )
        application_root = self._root / application
        _private_directory(application_root)
        destination = application_root / version
        published = RenderedArtifactBundle(
            cv_path=str(destination / "cv.pdf"),
            cover_letter_path=str(destination / "cover-letter.pdf"),
            cv_hash=rendered.cv_hash,
            cover_letter_hash=rendered.cover_letter_hash,
        )
        if destination.exists():
            if _published_matches(published):
                return published
            raise RuntimeError("Published artifact bundle contains different bytes")

        temporary = Path(tempfile.mkdtemp(prefix=".publish-", dir=application_root))
        os.chmod(temporary, 0o700)
        try:
            _copy_private(cv_source, temporary / "cv.pdf")
            _copy_private(cover_source, temporary / "cover-letter.pdf")
            if not _published_matches(
                RenderedArtifactBundle(
                    cv_path=str(temporary / "cv.pdf"),
                    cover_letter_path=str(temporary / "cover-letter.pdf"),
                    cv_hash=rendered.cv_hash,
                    cover_letter_hash=rendered.cover_letter_hash,
                )
            ):
                raise RuntimeError(
                    "Rendered artifact hashes changed during publication"
                )
            os.replace(temporary, destination)
            _fsync_directory(application_root)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return published

    def _trusted_staged_file(self, value: str, expected_hash: str) -> Path:
        path = Path(value)
        try:
            path.resolve().relative_to(self._staging.resolve())
        except ValueError:
            raise ValueError(
                "Rendered artifact is outside the private staging area"
            ) from None
        if not path.is_file() or _file_hash(path) != expected_hash:
            raise ValueError("Rendered artifact hash mismatch")
        return path


def _safe_component(value: str) -> str:
    candidate = str(value).strip()
    if candidate in {"", ".", ".."} or _SAFE_COMPONENT.fullmatch(candidate) is None:
        raise ValueError("Artifact identifier must be a safe path component")
    return candidate


def _write_pdf(path: Path, text: str, *, max_pages: int) -> None:
    temporary = path.with_suffix(".tmp")
    document = SimpleDocTemplate(
        str(temporary),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title="Application document",
        author="Candidate",
    )
    document.build(
        _document_story(text),
        canvasmaker=_invariant_canvas,
    )
    if len(PdfReader(temporary).pages) > max_pages:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"application document exceeds {max_pages} pages")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    _fsync_file(path)


def _document_story(text: str) -> list:
    styles = _document_styles()
    story: list = []
    before_first_section = True
    header_line = 0
    lines = str(text).splitlines() or [""]
    index = 0
    while index < len(lines):
        line = _safe_pdf_text(lines[index].strip())
        index += 1
        if not line:
            if header_line:
                before_first_section = False
                header_line = 0
            story.append(Spacer(1, 2.4 * mm))
            continue
        if line.startswith("### "):
            story.append(Paragraph(escape(line[4:]), styles["entry"]))
            metadata = []
            while index < len(lines) and len(metadata) < 3:
                candidate = _safe_pdf_text(lines[index].strip())
                if not candidate or candidate.startswith(("#", "- ")):
                    break
                metadata.append(candidate)
                index += 1
            if len(metadata) == 3:
                story.append(
                    Paragraph(
                        " | ".join(escape(item) for item in metadata),
                        styles["metadata"],
                    )
                )
            else:
                story.extend(
                    Paragraph(escape(item), styles["body"])
                    for item in metadata
                )
            continue
        if line.startswith("## "):
            before_first_section = False
            story.append(Paragraph(escape(line[3:]), styles["section"]))
            continue
        if line.startswith("# "):
            story.append(Paragraph(escape(line[2:]), styles["name"]))
            header_line = 1
            continue
        if line.startswith("- "):
            story.append(
                Paragraph(
                    escape(line[2:]),
                    styles["bullet"],
                    bulletText="-",
                )
            )
            continue
        if before_first_section and header_line:
            style = "headline" if header_line == 1 else "contact"
            story.append(Paragraph(escape(line), styles[style]))
            header_line += 1
            continue
        story.append(Paragraph(escape(line), styles["body"]))
    return story


def _document_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    ink = colors.HexColor("#14213D")
    accent = colors.HexColor("#2B6F77")
    muted = colors.HexColor("#4B5563")
    return {
        "name": ParagraphStyle(
            "CandidateName",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=19,
            leading=22,
            textColor=ink,
            alignment=1,
            spaceAfter=2,
        ),
        "headline": ParagraphStyle(
            "CandidateHeadline",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=10.5,
            leading=13,
            textColor=accent,
            alignment=1,
            spaceAfter=1,
        ),
        "contact": ParagraphStyle(
            "CandidateContact",
            parent=base["BodyText"],
            fontSize=8.5,
            leading=10.5,
            textColor=muted,
            alignment=1,
        ),
        "section": ParagraphStyle(
            "CvSection",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=12,
            textColor=accent,
            spaceBefore=7,
            spaceAfter=4,
            keepWithNext=True,
            borderWidth=0,
            borderPadding=0,
        ),
        "entry": ParagraphStyle(
            "CvEntry",
            parent=base["Heading3"],
            fontName="Helvetica-Bold",
            fontSize=9.4,
            leading=11.5,
            textColor=ink,
            spaceBefore=3,
            spaceAfter=1,
            keepWithNext=True,
        ),
        "body": ParagraphStyle(
            "CvBody",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=8.7,
            leading=11.2,
            textColor=colors.HexColor("#1F2937"),
            spaceAfter=1.5,
        ),
        "metadata": ParagraphStyle(
            "CvMetadata",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=8.3,
            leading=10.2,
            textColor=muted,
            spaceAfter=2.5,
            keepWithNext=True,
        ),
        "bullet": ParagraphStyle(
            "CvBullet",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=8.6,
            leading=11.2,
            textColor=colors.HexColor("#1F2937"),
            leftIndent=10,
            firstLineIndent=-7,
            bulletIndent=1,
            spaceAfter=2,
        ),
    }


def _invariant_canvas(*args, **kwargs):
    kwargs["invariant"] = 1
    kwargs["pageCompression"] = 1
    return Canvas(*args, **kwargs)


def _safe_pdf_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value))
    replacements = {
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "−": "-",
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
        "\u00ad": "",
    }
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    return normalized


def _copy_private(source: Path, destination: Path) -> None:
    shutil.copyfile(source, destination)
    os.chmod(destination, 0o600)
    _fsync_file(destination)


def _published_matches(bundle: RenderedArtifactBundle) -> bool:
    return all(
        path.is_file() and _file_hash(path) == expected
        for path, expected in (
            (Path(bundle.cv_path), bundle.cv_hash),
            (Path(bundle.cover_letter_path), bundle.cover_letter_hash),
        )
    )


def _file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = path
    while current.exists() and current != current.parent:
        if current == path or current.name in {".staging"}:
            os.chmod(current, 0o700)
        current = current.parent


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["LocalPdfArtifactRenderer"]
