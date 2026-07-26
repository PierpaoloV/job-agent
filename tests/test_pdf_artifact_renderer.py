import hashlib
import pathlib
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from pdf_artifact_renderer import LocalPdfArtifactRenderer  # noqa: E402


def digest(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_renderer_writes_and_atomically_publishes_real_pdf_bundle(tmp_path):
    renderer = LocalPdfArtifactRenderer(tmp_path / "artifacts")

    staged = renderer.render(
        application_id="synthetic-001",
        bundle_version="generation-v1",
        cv_text="Synthetic Candidate\nApplied AI researcher",
        cover_letter_text="Dear hiring team,\nI build trustworthy AI systems.",
    )
    published = renderer.publish(
        application_id="synthetic-001",
        bundle_version="bundle-v1",
        rendered=staged,
    )

    cv = pathlib.Path(published.cv_path)
    cover = pathlib.Path(published.cover_letter_path)
    assert cv.read_bytes().startswith(b"%PDF-")
    assert cover.read_bytes().startswith(b"%PDF-")
    assert (
        cv.parent
        == cover.parent
        == tmp_path / "artifacts" / "synthetic-001" / "bundle-v1"
    )
    assert published.cv_hash == digest(cv)
    assert published.cover_letter_hash == digest(cover)
    assert oct(cv.stat().st_mode & 0o777) == "0o600"
    assert oct(cv.parent.stat().st_mode & 0o777) == "0o700"
    assert oct((tmp_path / "artifacts").stat().st_mode & 0o777) == "0o700"


def test_publish_is_idempotent_but_rejects_conflicting_existing_bundle(tmp_path):
    renderer = LocalPdfArtifactRenderer(tmp_path / "artifacts")
    staged = renderer.render(
        application_id="synthetic-001",
        bundle_version="generation-v1",
        cv_text="CV v1",
        cover_letter_text="Letter v1",
    )
    first = renderer.publish(
        application_id="synthetic-001",
        bundle_version="bundle-v1",
        rendered=staged,
    )

    assert (
        renderer.publish(
            application_id="synthetic-001",
            bundle_version="bundle-v1",
            rendered=staged,
        )
        == first
    )

    changed = renderer.render(
        application_id="synthetic-001",
        bundle_version="generation-v2",
        cv_text="CV v2",
        cover_letter_text="Letter v2",
    )
    with pytest.raises(RuntimeError, match="different bytes"):
        renderer.publish(
            application_id="synthetic-001",
            bundle_version="bundle-v1",
            rendered=changed,
        )


@pytest.mark.parametrize("unsafe", ("../escape", "/absolute", "nested/path", ""))
def test_renderer_rejects_unsafe_path_components(tmp_path, unsafe):
    renderer = LocalPdfArtifactRenderer(tmp_path / "artifacts")

    with pytest.raises(ValueError, match="safe path component"):
        renderer.render(
            application_id=unsafe,
            bundle_version="generation-v1",
            cv_text="CV",
            cover_letter_text="Letter",
        )
