"""Authenticated, encrypted handoff of hosted artifacts to the local worker."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import base64
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any, Mapping
import zipfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from application_domain import PreparedArtifacts


HANDOFF_VERSION = "job-agent.artifact-handoff.v1"
_AAD = HANDOFF_VERSION.encode("ascii")
_MAGIC = b"JOBART1\x00"
_NONCE_SIZE = 12
_CANONICAL_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}")
_PACKAGE_FILES = frozenset(
    {"manifest.json", "prepared-artifacts.json", "cv.pdf", "cover-letter.pdf"}
)
_MANIFEST_FIELDS = frozenset(
    {
        "version",
        "identity",
        "authority",
        "artifact_version",
        "files",
        "manifest_digest",
    }
)
_PREPARED_FIELDS = frozenset(
    {
        "version",
        "cv_path",
        "cover_letter_path",
        "cv_hash",
        "cover_letter_hash",
        "evidence_source_version",
        "matrix_version",
        "family",
        "claims",
        "stretch_decision",
    }
)


@dataclass(frozen=True)
class ArtifactHandoffKey:
    value: bytes

    def __post_init__(self) -> None:
        if len(self.value) != 32:
            raise ValueError("Artifact handoff key must contain exactly 32 bytes")

    @classmethod
    def from_base64(cls, value: str) -> "ArtifactHandoffKey":
        try:
            decoded = base64.b64decode(
                str(value).strip().encode("ascii"), altchars=b"-_", validate=True
            )
        except (ValueError, UnicodeEncodeError):
            raise ValueError("Artifact handoff key must be valid base64") from None
        return cls(decoded)


@dataclass(frozen=True)
class ArtifactHandoffAuthority:
    repository: str
    workflow: str
    branch: str

    def __post_init__(self) -> None:
        values = (self.repository, self.workflow, self.branch)
        if any(not str(value).strip() for value in values):
            raise ValueError("Artifact handoff authority is incomplete")
        if any(str(value) != str(value).strip() for value in values):
            raise ValueError("Artifact handoff authority must be canonical")

    @classmethod
    def from_value(
        cls,
        value: "ArtifactHandoffAuthority | Mapping[str, str]",
    ) -> "ArtifactHandoffAuthority":
        if isinstance(value, cls):
            return value
        return cls(
            repository=str(value.get("repository", "")).strip(),
            workflow=str(value.get("workflow", "")).strip(),
            branch=str(value.get("branch", "")).strip(),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "repository": self.repository,
            "workflow": self.workflow,
            "branch": self.branch,
        }


@dataclass(frozen=True)
class ArtifactHandoffIdentity:
    application_id: str
    official_vacancy_version: str

    def __post_init__(self) -> None:
        _safe_component(self.application_id)
        if self.application_id != self.application_id.strip():
            raise ValueError("Artifact handoff application id must be canonical")
        if not _CANONICAL_SHA256.fullmatch(self.official_vacancy_version):
            raise ValueError(
                "Artifact handoff requires a canonical official vacancy version"
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "application_id": self.application_id,
            "official_vacancy_version": self.official_vacancy_version,
        }

    @property
    def artifact_name(self) -> str:
        digest = hashlib.sha256(_canonical_json(self.to_dict())).hexdigest()
        return f"application-artifacts-{digest}"


@dataclass(frozen=True)
class EncryptedArtifactPackage:
    path: Path
    package_hash: str


class HostedArtifactHandoff:
    """Export one immutable artifact bundle without exposing its plaintext."""

    def __init__(
        self,
        *,
        key: ArtifactHandoffKey,
        authority: ArtifactHandoffAuthority | Mapping[str, str],
    ) -> None:
        self._key = key
        self._authority = ArtifactHandoffAuthority.from_value(authority)

    def export(
        self,
        *,
        identity: ArtifactHandoffIdentity,
        artifacts: PreparedArtifacts,
        destination: Path,
    ) -> EncryptedArtifactPackage:
        if not _CANONICAL_SHA256.fullmatch(artifacts.version):
            raise ValueError("Artifact version is not canonical")
        cv = _verified_file(artifacts.cv_path, artifacts.cv_hash)
        cover = _verified_file(
            artifacts.cover_letter_path, artifacts.cover_letter_hash
        )
        prepared_payload = {
            **asdict(artifacts),
            "cv_path": "cv.pdf",
            "cover_letter_path": "cover-letter.pdf",
        }
        prepared_bytes = _canonical_json(prepared_payload)
        files = {
            "prepared-artifacts.json": _hash_bytes(prepared_bytes),
            "cv.pdf": _hash_bytes(cv.read_bytes()),
            "cover-letter.pdf": _hash_bytes(cover.read_bytes()),
        }
        manifest = {
            "version": HANDOFF_VERSION,
            "identity": identity.to_dict(),
            "authority": self._authority.to_dict(),
            "artifact_version": artifacts.version,
            "files": files,
        }
        manifest["manifest_digest"] = _manifest_digest(manifest)
        plaintext = _archive(
            {
                "manifest.json": _canonical_json(manifest),
                "prepared-artifacts.json": prepared_bytes,
                "cv.pdf": cv.read_bytes(),
                "cover-letter.pdf": cover.read_bytes(),
            }
        )
        nonce = os.urandom(_NONCE_SIZE)
        encrypted = _MAGIC + nonce + AESGCM(self._key.value).encrypt(
            nonce, plaintext, _AAD
        )
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(raw_temporary)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            _fsync_directory(path.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return EncryptedArtifactPackage(path=path, package_hash=_file_hash(path))


class LocalArtifactHandoff:
    """Install only an authenticated package with the exact expected identity."""

    def __init__(
        self,
        *,
        key: ArtifactHandoffKey,
        expected_authority: ArtifactHandoffAuthority | Mapping[str, str],
        root: Path,
    ) -> None:
        self._key = key
        self._expected_authority = ArtifactHandoffAuthority.from_value(
            expected_authority
        )
        configured_root = Path(root).expanduser()
        self._root = (
            configured_root
            if configured_root.is_absolute()
            else Path.cwd() / configured_root
        )

    def install(
        self,
        package: Path,
        *,
        expected_identity: ArtifactHandoffIdentity,
    ) -> PreparedArtifacts:
        entries = self._decrypt_entries(Path(package))
        manifest = _json_mapping(entries["manifest.json"], "handoff manifest")
        self._validate_manifest(manifest, entries, expected_identity)
        prepared_value = _json_mapping(
            entries["prepared-artifacts.json"], "prepared artifacts"
        )
        if set(prepared_value) != _PREPARED_FIELDS:
            raise ValueError("Prepared artifact metadata schema is invalid")
        if prepared_value.get("cv_path") != "cv.pdf" or (
            prepared_value.get("cover_letter_path") != "cover-letter.pdf"
        ):
            raise ValueError("Hosted artifact paths are not canonical")
        artifacts = PreparedArtifacts.from_dict(prepared_value)
        if not _CANONICAL_SHA256.fullmatch(artifacts.version):
            raise ValueError("Artifact version is not canonical")
        if artifacts.version != manifest["artifact_version"]:
            raise ValueError("Artifact version does not match handoff manifest")
        if artifacts.cv_hash != _hash_bytes(entries["cv.pdf"]) or (
            artifacts.cover_letter_hash != _hash_bytes(entries["cover-letter.pdf"])
        ):
            raise ValueError("Prepared artifact hashes do not match package bytes")
        destination = (
            self._root
            / _safe_component(expected_identity.application_id)
            / _safe_component(artifacts.version)
        )
        installed = replace(
            artifacts,
            cv_path=str(destination / "cv.pdf"),
            cover_letter_path=str(destination / "cover-letter.pdf"),
        )
        if destination.exists():
            if self.verify_installed(expected_identity, installed):
                return installed
            raise RuntimeError("Installed artifact bundle contains different bytes")
        _ensure_private_directory(self._root)
        _ensure_private_directory(destination.parent)
        temporary = Path(
            tempfile.mkdtemp(prefix=".install-", dir=destination.parent)
        )
        os.chmod(temporary, 0o700)
        try:
            _write_private(temporary / "cv.pdf", entries["cv.pdf"])
            _write_private(
                temporary / "cover-letter.pdf", entries["cover-letter.pdf"]
            )
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        if not self.verify_installed(expected_identity, installed):
            raise RuntimeError("Installed artifact bundle failed verification")
        return installed

    def verify_installed(
        self,
        identity: ArtifactHandoffIdentity,
        artifacts: PreparedArtifacts,
    ) -> bool:
        try:
            destination = (
                self._root
                / _safe_component(identity.application_id)
                / _safe_component(artifacts.version)
            )
        except ValueError:
            return False
        if not _CANONICAL_SHA256.fullmatch(artifacts.version):
            return False
        if Path(artifacts.cv_path) != destination / "cv.pdf" or Path(
            artifacts.cover_letter_path
        ) != destination / "cover-letter.pdf":
            return False
        directories = (self._root, destination.parent, destination)
        if not all(_is_owner_only_directory(path) for path in directories):
            return False
        return all(
            _private_installed_file_matches(Path(path), expected)
            for path, expected in (
                (artifacts.cv_path, artifacts.cv_hash),
                (artifacts.cover_letter_path, artifacts.cover_letter_hash),
            )
        )

    def _decrypt_entries(self, package: Path) -> dict[str, bytes]:
        value = package.read_bytes()
        if not value.startswith(_MAGIC) or len(value) <= len(_MAGIC) + _NONCE_SIZE:
            raise ValueError("Unsupported encrypted artifact package")
        nonce_start = len(_MAGIC)
        nonce = value[nonce_start : nonce_start + _NONCE_SIZE]
        ciphertext = value[nonce_start + _NONCE_SIZE :]
        try:
            plaintext = AESGCM(self._key.value).decrypt(nonce, ciphertext, _AAD)
        except Exception:
            raise ValueError("Encrypted artifact package authentication failed") from None
        try:
            with zipfile.ZipFile(BytesIO(plaintext)) as archive:
                entries = archive.infolist()
                names = [entry.filename for entry in entries]
                if (
                    len(entries) != len(_PACKAGE_FILES)
                    or len(names) != len(set(names))
                    or set(names) != _PACKAGE_FILES
                    or any(entry.is_dir() for entry in entries)
                ):
                    raise ValueError
                return {name: archive.read(name) for name in names}
        except (ValueError, zipfile.BadZipFile, KeyError):
            raise ValueError(
                "Encrypted artifact package contents are invalid"
            ) from None

    def _validate_manifest(
        self,
        manifest: Mapping[str, Any],
        entries: Mapping[str, bytes],
        expected_identity: ArtifactHandoffIdentity,
    ) -> None:
        if set(manifest) != _MANIFEST_FIELDS:
            raise ValueError("Artifact handoff manifest schema is invalid")
        if manifest.get("version") != HANDOFF_VERSION:
            raise ValueError("Unsupported artifact handoff version")
        if manifest.get("manifest_digest") != _manifest_digest(manifest):
            raise ValueError("Artifact handoff manifest digest mismatch")
        if manifest.get("identity") != expected_identity.to_dict():
            raise ValueError("Artifact handoff identity mismatch")
        if manifest.get("authority") != self._expected_authority.to_dict():
            raise ValueError("Artifact handoff authority mismatch")
        if not _CANONICAL_SHA256.fullmatch(str(manifest.get("artifact_version", ""))):
            raise ValueError("Artifact handoff artifact version is invalid")
        files = manifest.get("files")
        if not isinstance(files, Mapping) or set(files) != (
            _PACKAGE_FILES - {"manifest.json"}
        ):
            raise ValueError("Artifact handoff file manifest is invalid")
        for name, expected_hash in files.items():
            if _hash_bytes(entries[name]) != expected_hash:
                raise ValueError("Artifact handoff file hash mismatch")


def _archive(entries: Mapping[str, bytes]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            archive.writestr(name, entries[name])
    return output.getvalue()


def _manifest_digest(value: Mapping[str, Any]) -> str:
    canonical = {
        key: item for key, item in value.items() if key != "manifest_digest"
    }
    return _hash_bytes(_canonical_json(canonical))


def _json_mapping(value: bytes, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(f"{label} is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be an object")
    return parsed


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _verified_file(value: str, expected_hash: str) -> Path:
    path = Path(value)
    if not path.is_file() or _file_hash(path) != expected_hash:
        raise ValueError("Prepared artifact file hash mismatch")
    return path


def _safe_component(value: str) -> str:
    candidate = str(value).strip()
    if candidate in {"", ".", ".."} or _SAFE_COMPONENT.fullmatch(candidate) is None:
        raise ValueError("Artifact handoff identifier must be a safe path component")
    return candidate


def _write_private(path: Path, value: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(path, 0o600)


def _installed_matches(artifacts: PreparedArtifacts) -> bool:
    return prepared_artifacts_are_intact(artifacts)


def prepared_artifacts_are_intact(artifacts: PreparedArtifacts) -> bool:
    return all(
        _is_regular_file_without_symlink(Path(path))
        and _file_hash(Path(path)) == expected
        for path, expected in (
            (artifacts.cv_path, artifacts.cv_hash),
            (artifacts.cover_letter_path, artifacts.cover_letter_hash),
        )
    )


def encrypted_package_has_supported_header(value: bytes) -> bool:
    return value.startswith(_MAGIC) and len(value) > len(_MAGIC) + _NONCE_SIZE


def _ensure_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=False, mode=0o700)
        metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ValueError("Artifact install directory is invalid")
    os.chmod(path, 0o700)
    if not _is_owner_only_directory(path):
        raise ValueError("Artifact install directory is not owner-only")
    _fsync_directory(path)
    if path.parent != path:
        _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_regular_file_without_symlink(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _is_owner_only_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
    )


def _private_installed_file_matches(path: Path, expected_hash: str) -> bool:
    descriptor = None
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return False
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            return False
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        return f"sha256:{digest.hexdigest()}" == expected_hash
    except (FileNotFoundError, OSError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _file_hash(path: Path) -> str:
    return _hash_bytes(path.read_bytes())


__all__ = [
    "ArtifactHandoffAuthority",
    "ArtifactHandoffIdentity",
    "ArtifactHandoffKey",
    "EncryptedArtifactPackage",
    "HostedArtifactHandoff",
    "LocalArtifactHandoff",
    "encrypted_package_has_supported_header",
    "prepared_artifacts_are_intact",
]
