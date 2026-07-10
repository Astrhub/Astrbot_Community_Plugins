from __future__ import annotations

import hashlib
import mimetypes
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import yaml
from packaging.version import InvalidVersion, Version

from ..config import ArtifactSettings

PLUGIN_NAME_PATTERN = re.compile(r"^astrbot_plugin_[a-z0-9_]+$")
GITHUB_HOSTS = {"github.com", "www.github.com"}
NATIVE_SUFFIXES = {
    ".a",
    ".app",
    ".bin",
    ".com",
    ".dll",
    ".dylib",
    ".exe",
    ".msi",
    ".o",
    ".pyd",
    ".so",
}
TEXT_SUFFIXES = {
    ".cfg",
    ".css",
    ".csv",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".rst",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".vue",
    ".yaml",
    ".yml",
}
MAX_METADATA_BYTES = 256 * 1024
LANGUAGE_BY_SUFFIX = {
    ".css": "css",
    ".html": "html",
    ".js": "javascript",
    ".json": "json",
    ".md": "markdown",
    ".py": "python",
    ".sh": "shell",
    ".toml": "toml",
    ".ts": "typescript",
    ".vue": "vue",
    ".yaml": "yaml",
    ".yml": "yaml",
}


class PrecheckError(ValueError):
    def __init__(self, code: str, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True, slots=True)
class ArchiveMember:
    path: str
    source_name: str
    language: str
    mime_type: str
    sha256: str
    size_bytes: int
    line_count: int | None
    is_text: bool

    def as_manifest(self, *, content_key: str | None = None) -> dict[str, Any]:
        return {
            "path": self.path,
            "language": self.language,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "line_count": self.line_count,
            "is_text": self.is_text,
            "content_key": content_key,
            "flags": {},
        }


@dataclass(frozen=True, slots=True)
class PrecheckResult:
    metadata: dict[str, Any]
    version: str
    normalized_version: str
    tree_sha256: str
    members: tuple[ArchiveMember, ...]


class ArchivePrechecker:
    def __init__(self, settings: ArtifactSettings) -> None:
        self.settings = settings

    def inspect(self, archive_path: Path, *, expected_repo: str) -> PrecheckResult:
        try:
            archive = zipfile.ZipFile(archive_path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise PrecheckError("invalid_zip", "插件包不是可读取的 ZIP 文件") from exc

        with archive:
            files = [item for item in archive.infolist() if not item.is_dir()]
            if len(files) > self.settings.max_files:
                raise PrecheckError("too_many_files", "插件包文件数量超过限制")
            if not files:
                raise PrecheckError("empty_archive", "插件包为空")

            normalized = [(item, _normalize_member_path(item.filename)) for item in files]
            prefix = _common_wrapper_prefix([path for _, path in normalized])
            seen: set[str] = set()
            seen_casefold: set[str] = set()
            total_unpacked = 0
            members: list[ArchiveMember] = []
            content_by_path: dict[str, bytes] = {}

            for info, raw_path in normalized:
                path = _strip_prefix(raw_path, prefix)
                if not path:
                    raise PrecheckError("invalid_path", "插件包包含空文件路径")
                _validate_depth(path, self.settings.max_path_depth)
                folded = path.casefold()
                if path in seen or folded in seen_casefold:
                    raise PrecheckError(
                        "duplicate_path", "插件包包含重复或大小写冲突路径", path=path
                    )
                seen.add(path)
                seen_casefold.add(folded)
                if info.flag_bits & 0x1:
                    raise PrecheckError("encrypted_entry", "插件包不能包含加密文件", path=path)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise PrecheckError("symlink_not_allowed", "插件包不能包含符号链接", path=path)
                if info.file_size > self.settings.max_file_bytes:
                    raise PrecheckError("file_too_large", "插件包中的单个文件超过限制", path=path)
                total_unpacked += info.file_size
                if total_unpacked > self.settings.max_unpacked_bytes:
                    raise PrecheckError("archive_unpacked_too_large", "插件包解压后大小超过限制")
                ratio = info.file_size / max(info.compress_size, 1)
                if ratio > self.settings.max_compression_ratio:
                    raise PrecheckError("zip_bomb_suspected", "插件包压缩比异常", path=path)

                suffix = PurePosixPath(path).suffix.lower()
                if suffix in NATIVE_SUFFIXES:
                    raise PrecheckError(
                        "native_binary_not_supported",
                        "P1 不接受原生可执行制品",
                        path=path,
                    )
                try:
                    content = archive.read(info)
                except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
                    raise PrecheckError("zip_read_failed", "插件包文件读取失败", path=path) from exc
                if len(content) != info.file_size:
                    raise PrecheckError("zip_size_mismatch", "插件包文件大小校验失败", path=path)
                if content.startswith(b"version https://git-lfs.github.com/spec/v1"):
                    raise PrecheckError(
                        "git_lfs_not_supported", "P1 不支持 Git LFS 指针", path=path
                    )
                if path == ".gitmodules":
                    raise PrecheckError("submodule_not_supported", "P1 不支持 Git submodule")

                is_text, line_count = _text_details(path, content)
                if is_text:
                    content_by_path[path] = content
                members.append(
                    ArchiveMember(
                        path=path,
                        source_name=info.filename,
                        language=LANGUAGE_BY_SUFFIX.get(suffix, "text" if is_text else ""),
                        mime_type=mimetypes.guess_type(path)[0]
                        or ("text/plain" if is_text else "application/octet-stream"),
                        sha256=hashlib.sha256(content).hexdigest(),
                        size_bytes=len(content),
                        line_count=line_count,
                        is_text=is_text,
                    )
                )

            metadata_paths = [path for path in ("metadata.yaml", "metadata.yml") if path in seen]
            if not metadata_paths:
                raise PrecheckError("metadata_missing", "插件包根目录缺少 metadata.yaml/yml")
            if len(metadata_paths) > 1:
                raise PrecheckError(
                    "metadata_ambiguous", "插件包同时包含 metadata.yaml 和 metadata.yml"
                )
            if "main.py" not in seen:
                raise PrecheckError("entrypoint_missing", "插件包根目录缺少 main.py")
            metadata = _parse_metadata(content_by_path.get(metadata_paths[0], b""))
            version, normalized_version = _validate_metadata(metadata, expected_repo=expected_repo)
            tree_sha256 = _tree_digest(members)
            return PrecheckResult(
                metadata=metadata,
                version=version,
                normalized_version=normalized_version,
                tree_sha256=tree_sha256,
                members=tuple(sorted(members, key=lambda item: item.path)),
            )


def read_member(archive_path: Path, source_name: str) -> bytes:
    with zipfile.ZipFile(archive_path) as archive:
        return archive.read(source_name)


def normalize_version(value: str) -> str:
    raw = str(value or "").strip()
    candidate = raw[1:] if raw[:1].lower() == "v" else raw
    try:
        return str(Version(candidate))
    except InvalidVersion as exc:
        raise PrecheckError("version_invalid", "metadata 版本号无法解析") from exc


def normalize_github_repo(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme != "https" or parsed.hostname not in GITHUB_HOSTS:
        raise PrecheckError("repo_invalid", "metadata repo 必须是公开 GitHub HTTPS 地址")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise PrecheckError("repo_invalid", "metadata repo 必须指向 GitHub 仓库根地址")
    owner, repo = parts
    repo = repo.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise PrecheckError("repo_invalid", "metadata repo 地址不合规")
    return f"https://github.com/{owner.lower()}/{repo.lower()}"


def github_repo_name(value: str) -> str:
    return normalize_github_repo(value).rsplit("/", 1)[-1]


def _normalize_member_path(value: str) -> str:
    if not value or "\x00" in value or "\\" in value:
        raise PrecheckError("path_traversal", "插件包包含非法路径")
    normalized = unicodedata.normalize("NFC", value)
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PrecheckError("path_traversal", "插件包包含路径穿越")
    if path.parts and re.match(r"^[A-Za-z]:", path.parts[0]):
        raise PrecheckError("path_traversal", "插件包包含绝对路径")
    return path.as_posix()


def _common_wrapper_prefix(paths: list[str]) -> str:
    if any(path in {"metadata.yaml", "metadata.yml", "main.py"} for path in paths):
        return ""
    split_paths = [PurePosixPath(path).parts for path in paths]
    if all(len(parts) >= 2 for parts in split_paths):
        roots = {parts[0] for parts in split_paths}
        if len(roots) == 1:
            return next(iter(roots))
    return ""


def _strip_prefix(path: str, prefix: str) -> str:
    if not prefix:
        return path
    marker = f"{prefix}/"
    return path[len(marker) :] if path.startswith(marker) else path


def _validate_depth(path: str, maximum: int) -> None:
    if len(PurePosixPath(path).parts) > maximum:
        raise PrecheckError("path_too_deep", "插件包目录层级超过限制", path=path)


def _text_details(path: str, content: bytes) -> tuple[bool, int | None]:
    suffix = PurePosixPath(path).suffix.lower()
    if b"\x00" in content or (suffix not in TEXT_SUFFIXES and not path.startswith("README")):
        return False, None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return False, None
    return True, len(text.splitlines())


def _parse_metadata(content: bytes) -> dict[str, Any]:
    if len(content) > MAX_METADATA_BYTES:
        raise PrecheckError("metadata_too_large", "metadata.yaml 超过大小限制")
    try:
        text = content.decode("utf-8")
        if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text)):
            raise PrecheckError("metadata_alias_not_allowed", "metadata.yaml 不能使用 YAML alias")
        payload = yaml.load(text, Loader=_UniqueKeyLoader)
    except PrecheckError:
        raise
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PrecheckError("metadata_invalid", "metadata.yaml 无法安全解析") from exc
    if not isinstance(payload, dict):
        raise PrecheckError("metadata_invalid", "metadata.yaml 顶层必须是对象")
    return {str(key): value for key, value in payload.items()}


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, (str, int, float, bool, type(None))):
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "metadata key must be a scalar",
                    key_node.start_mark,
                )
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"duplicate key: {key}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _validate_metadata(metadata: dict[str, Any], *, expected_repo: str) -> tuple[str, str]:
    required = ("name", "display_name", "desc", "version", "author", "repo")
    missing = [key for key in required if not str(metadata.get(key) or "").strip()]
    if missing:
        raise PrecheckError(
            "metadata_required_field_missing",
            "metadata 缺少必填字段：" + ", ".join(missing),
        )
    name = str(metadata["name"]).strip()
    if not PLUGIN_NAME_PATTERN.fullmatch(name):
        raise PrecheckError("plugin_name_invalid", "插件名必须使用 astrbot_plugin_ 小写命名")
    actual_repo = normalize_github_repo(str(metadata["repo"]))
    if actual_repo != normalize_github_repo(expected_repo):
        raise PrecheckError("metadata_repo_mismatch", "metadata repo 与登记仓库不一致")
    version = str(metadata["version"]).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", version):
        raise PrecheckError("version_path_unsafe", "metadata 版本号不能安全用于 CDN 路径")
    return version, normalize_version(version)


def _tree_digest(members: list[ArchiveMember]) -> str:
    digest = hashlib.sha256()
    for member in sorted(members, key=lambda item: item.path):
        digest.update(member.path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(member.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()
