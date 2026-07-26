"""Local, deterministic PDF rendering for private application artifacts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile

from reportlab.lib.pagesizes import A4
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
        _write_pdf(cv, cv_text)
        _write_pdf(cover_letter, cover_letter_text)
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


def _write_pdf(path: Path, text: str) -> None:
    temporary = path.with_suffix(".tmp")
    canvas = Canvas(str(temporary), pagesize=A4, invariant=1, pageCompression=1)
    width, height = A4
    margin = 54
    y = height - margin
    canvas.setFont("Helvetica", 10)
    for paragraph in str(text).splitlines() or [""]:
        for line in _wrap(paragraph, 92) or [""]:
            if y < margin:
                canvas.showPage()
                canvas.setFont("Helvetica", 10)
                y = height - margin
            canvas.drawString(margin, y, line)
            y -= 14
        y -= 4
    canvas.save()
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    _fsync_file(path)


def _wrap(value: str, limit: int) -> tuple[str, ...]:
    words = value.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > limit:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return tuple(lines)


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
