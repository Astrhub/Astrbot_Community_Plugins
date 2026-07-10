from __future__ import annotations

import ast
import hashlib
import re
import zipfile
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any, Iterable

from .archive import ArchiveMember
from .models import highest_risk

RULESET_VERSION = "p1.1"
URL_PATTERN = re.compile(r"https?://", re.IGNORECASE)
LONG_ENCODED_PATTERN = re.compile(r"[A-Za-z0-9+/]{512,}={0,2}")
SENSITIVE_PATTERN = re.compile(
    r"(?:\.ssh|\.aws|credentials|id_rsa|private[_-]?key|api[_-]?key|access[_-]?token)",
    re.IGNORECASE,
)
DIRECT_REQUIREMENT_PATTERN = re.compile(
    r"^(?:-e\s+|--editable\s+|(?:git|hg|svn|bzr)\+|https?://|file:|\.{0,2}/)",
    re.IGNORECASE,
)


class StaticScanner:
    def scan(self, archive_path: str, members: Iterable[ArchiveMember]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        with zipfile.ZipFile(archive_path) as archive:
            for member in members:
                if not member.is_text:
                    continue
                content = archive.read(member.source_name)
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                suffix = PurePosixPath(member.path).suffix.lower()
                if suffix == ".py":
                    findings.extend(_scan_python(member.path, text))
                if PurePosixPath(member.path).name.lower() in {
                    "requirements.txt",
                    "requirements-dev.txt",
                }:
                    findings.extend(_scan_requirements(member.path, text))
        return _deduplicate(findings)

    @staticmethod
    def risk_level(findings: Iterable[dict[str, Any]]) -> str:
        return highest_risk([str(item["severity"]) for item in findings]).value


def _scan_python(path: str, source: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [
            _finding(
                rule_id="PY000",
                severity="high",
                category="syntax",
                path=path,
                line=exc.lineno,
                message="Python 文件无法解析，插件很可能无法导入",
                suggestion="修复语法错误后重新提交",
                evidence=_line_excerpt(source, exc.lineno),
            )
        ]

    findings: list[dict[str, Any]] = []
    call_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        call_names.add(name)
        line = getattr(node, "lineno", None)
        evidence = _line_excerpt(source, line)
        if name in {"eval", "exec", "compile", "builtins.eval", "builtins.exec"}:
            findings.append(
                _finding(
                    rule_id="PY001",
                    severity="high",
                    category="dynamic_execution",
                    path=path,
                    line=line,
                    message=f"检测到动态代码执行：{name}",
                    suggestion="改为显式解析和固定分支，避免执行外部输入",
                    evidence=evidence,
                )
            )
        elif name == "os.system" or name.startswith("subprocess."):
            findings.append(
                _finding(
                    rule_id="PY002",
                    severity="high",
                    category="process_execution",
                    path=path,
                    line=line,
                    message=f"检测到系统命令或子进程调用：{name}",
                    suggestion="说明必要性并限制命令、参数、工作目录和输入来源",
                    evidence=evidence,
                )
            )
        elif name in {
            "__import__",
            "importlib.import_module",
            "importlib.util.spec_from_file_location",
        }:
            findings.append(
                _finding(
                    rule_id="PY003",
                    severity="medium",
                    category="dynamic_import",
                    path=path,
                    line=line,
                    message=f"检测到动态模块加载：{name}",
                    suggestion="使用固定导入或严格白名单",
                    evidence=evidence,
                )
            )
        elif name in {"marshal.loads", "pickle.loads", "cloudpickle.loads"}:
            findings.append(
                _finding(
                    rule_id="PY005",
                    severity="critical" if name == "marshal.loads" else "high",
                    category="unsafe_deserialization",
                    path=path,
                    line=line,
                    message=f"检测到高风险反序列化：{name}",
                    suggestion="使用 JSON 等不具备代码执行能力的格式",
                    evidence=evidence,
                )
            )

    lowered = source.lower()
    has_download = bool(URL_PATTERN.search(source)) and any(
        name.startswith(prefix)
        for name in call_names
        for prefix in ("requests.", "httpx.", "urllib.request.", "aiohttp.")
    )
    has_execute = any(
        name in {"exec", "eval", "os.system"} or name.startswith("subprocess.")
        for name in call_names
    )
    if has_download and has_execute:
        findings.append(
            _finding(
                rule_id="PY003",
                severity="critical",
                category="download_and_execute",
                path=path,
                line=None,
                message="同一文件同时包含网络下载与代码/命令执行能力",
                suggestion="禁止执行远程内容；如确有需要，使用固定摘要和显式人工确认",
                evidence="package-level heuristic: network + execution",
            )
        )
    sensitive = SENSITIVE_PATTERN.search(source)
    if sensitive:
        findings.append(
            _finding(
                rule_id="PY004",
                severity="high",
                category="sensitive_data_access",
                path=path,
                line=source[: sensitive.start()].count("\n") + 1,
                message="检测到凭据或敏感目录访问模式",
                suggestion="仅通过插件配置读取必要凭据，不扫描用户主目录",
                evidence=_line_excerpt(source, source[: sensitive.start()].count("\n") + 1),
            )
        )
    obfuscation_terms = sum(
        term in lowered
        for term in ("base64.b64decode", "zlib.decompress", "marshal.loads", "codecs.decode")
    )
    if obfuscation_terms >= 2 or LONG_ENCODED_PATTERN.search(source):
        findings.append(
            _finding(
                rule_id="PY005",
                severity="critical" if obfuscation_terms >= 2 else "high",
                category="obfuscation",
                path=path,
                line=None,
                message="检测到组合解码、压缩或长编码载荷",
                suggestion="提交可直接审阅的源码并移除隐藏载荷",
                evidence="package-level heuristic: encoded payload",
            )
        )
    return findings


def _scan_requirements(path: str, source: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    packages: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if DIRECT_REQUIREMENT_PATTERN.search(line) or " @ " in line:
            findings.append(
                _finding(
                    rule_id="REQ001",
                    severity="high",
                    category="dependency_source",
                    path=path,
                    line=line_number,
                    message="requirements 包含 VCS、URL、editable 或本地路径依赖",
                    suggestion="改用 PyPI 上带固定版本范围的依赖",
                    evidence=_limited_excerpt(line),
                )
            )
        name_match = re.match(r"^([A-Za-z0-9_.-]+)", line)
        if name_match:
            normalized = re.sub(r"[-_.]+", "-", name_match.group(1)).lower()
            packages[normalized].append((line_number, line))
    for package, declarations in packages.items():
        unique = {line for _, line in declarations}
        if len(unique) <= 1:
            continue
        line_number, line = declarations[-1]
        findings.append(
            _finding(
                rule_id="REQ002",
                severity="medium",
                category="dependency_conflict",
                path=path,
                line=line_number,
                message=f"依赖 {package} 存在多个不一致声明",
                suggestion="合并为单一、可解析的版本范围",
                evidence=_limited_excerpt(line),
            )
        )
    return findings


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _finding(
    *,
    rule_id: str,
    severity: str,
    category: str,
    path: str,
    line: int | None,
    message: str,
    suggestion: str,
    evidence: str,
) -> dict[str, Any]:
    fingerprint_source = f"{rule_id}\x00{path}\x00{line or 0}\x00{message}"
    return {
        "fingerprint": hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest(),
        "rule_id": rule_id,
        "file_path": path,
        "line_start": line,
        "line_end": line,
        "severity": severity,
        "category": category,
        "message": message,
        "suggestion": suggestion,
        "evidence_excerpt": _limited_excerpt(evidence),
        "confidence": 0.85,
        "status": "open",
        "metadata": {"ruleset_version": RULESET_VERSION},
    }


def _line_excerpt(source: str, line: int | None) -> str:
    if not line or line < 1:
        return ""
    lines = source.splitlines()
    if line > len(lines):
        return ""
    return _limited_excerpt(lines[line - 1].strip())


def _limited_excerpt(value: str) -> str:
    cleaned = " ".join(str(value or "").split())
    return cleaned[:240]


def _deduplicate(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for finding in findings:
        unique[str(finding["fingerprint"])] = finding
    return list(unique.values())
