from __future__ import annotations

import ast
import hashlib
import re
import zipfile
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any

from .archive import ArchiveMember, is_excluded_review_path
from .models import highest_risk
from .requirements_parser import RequirementsParseError, parse_requirements

RULESET_VERSION = "p1.2"
URL_PATTERN = re.compile(r"https?://", re.IGNORECASE)
LONG_ENCODED_PATTERN = re.compile(r"[A-Za-z0-9+/]{512,}={0,2}")
_DATA_URI_PREFIX = re.compile(
    r"^data:(?:image|audio|video|font)/[a-z0-9.+-]+;base64,", re.IGNORECASE
)
_SENSITIVE_PATH_PATTERNS = (
    re.compile(r"(?:^|[/\\~])\.ssh(?:[/\\]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[/\\~])\.aws(?:[/\\]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[/\\~])(?:id_rsa|id_ed25519|id_ecdsa|id_dsa)$", re.IGNORECASE),
    re.compile(r"(?:^|[/\\~])\.gnupg(?:[/\\]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[/\\~])\.kube[/\\]config$", re.IGNORECASE),
    re.compile(r"(?:^|[/\\~])\.docker[/\\]config\.json$", re.IGNORECASE),
    re.compile(r"(?:^|[/\\~])\.git-credentials$", re.IGNORECASE),
    re.compile(r"(?:^|[/\\~])\.netrc$", re.IGNORECASE),
    re.compile(r"^/etc/(?:shadow|passwd|sudoers)$"),
)
_HARDCODED_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("api_secret_key", re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b")),
)
_DYNAMIC_EXEC_NAMES = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "builtins.eval",
        "builtins.exec",
        "builtins.compile",
    }
)
_PROCESS_EXEC_NAMES = frozenset(
    {
        "os.system",
        "os.popen",
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
    }
)
_DYNAMIC_IMPORT_NAMES = frozenset(
    {
        "__import__",
        "builtins.__import__",
        "importlib.import_module",
        "importlib.util.spec_from_file_location",
    }
)
_UNSAFE_DESERIALIZATION_NAMES = frozenset(
    {
        "marshal.loads",
        "pickle.loads",
        "pickle.load",
        "cloudpickle.loads",
        "dill.loads",
    }
)
_DECODE_CALL_NAMES = frozenset(
    {
        "base64.b64decode",
        "base64.urlsafe_b64decode",
        "zlib.decompress",
        "gzip.decompress",
        "lzma.decompress",
        "bz2.decompress",
        "marshal.loads",
        "codecs.decode",
    }
)
_NETWORK_PREFIXES = ("requests.", "httpx.", "urllib.request.", "aiohttp.")
_SHELL_OR_INTERPRETER_BINS = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "dash",
        "ash",
        "cmd",
        "cmd.exe",
        "powershell",
        "pwsh",
        "python",
        "python3",
        "node",
        "perl",
        "ruby",
    }
)


class StaticScanner:
    def scan(self, archive_path: str, members: Iterable[ArchiveMember]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        with zipfile.ZipFile(archive_path) as archive:
            for member in members:
                if not member.is_text or is_excluded_review_path(member.path):
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

    aliases = _imported_aliases(tree)
    docstring_ids = _docstring_node_ids(tree)
    nested_compile_ids = _nested_compile_call_ids(tree, aliases)

    findings: list[dict[str, Any]] = []
    call_names: set[str] = set()
    has_dangerous_execute = False
    has_dynamic_exec = False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _resolve_call_name(node.func, aliases)
        if not name:
            continue
        call_names.add(name)
        line = getattr(node, "lineno", None)
        evidence = _line_excerpt(source, line)

        if name in _DYNAMIC_EXEC_NAMES:
            if name in {"compile", "builtins.compile"} and id(node) in nested_compile_ids:
                continue
            has_dynamic_exec = True
            if name not in {"compile", "builtins.compile"}:
                has_dangerous_execute = True
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
        elif name in _PROCESS_EXEC_NAMES:
            if _is_dangerous_process_call(node, name):
                has_dangerous_execute = True
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
        elif name in _DYNAMIC_IMPORT_NAMES:
            if _is_constant_module_import(node, name):
                continue
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
        elif name in _UNSAFE_DESERIALIZATION_NAMES:
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

    literals = _executable_string_literals(tree, docstring_ids)
    has_url_literal = any(URL_PATTERN.search(value) for _, value in literals)
    has_network_call = any(
        name.startswith(prefix) for name in call_names for prefix in _NETWORK_PREFIXES
    )
    if has_url_literal and has_network_call and has_dangerous_execute:
        findings.append(
            _finding(
                rule_id="PY006",
                severity="critical",
                category="download_and_execute",
                path=path,
                line=None,
                message="同一文件同时包含网络下载与代码/命令执行能力",
                suggestion="禁止执行远程内容；如确有需要，使用固定摘要和显式人工确认",
                evidence="package-level heuristic: network + execution",
            )
        )

    sensitive_hit = _first_sensitive_path_literal(literals)
    if sensitive_hit is not None:
        line, _ = sensitive_hit
        findings.append(
            _finding(
                rule_id="PY004",
                severity="high",
                category="sensitive_data_access",
                path=path,
                line=line,
                message="检测到敏感凭据目录或系统密钥文件访问模式",
                suggestion="仅通过插件配置读取必要凭据，不扫描用户主目录或系统敏感文件",
                evidence=_line_excerpt(source, line),
            )
        )

    secret_hit = _first_hardcoded_secret_literal(literals)
    if secret_hit is not None:
        line, secret_kind = secret_hit
        findings.append(
            _finding(
                rule_id="PY007",
                severity="high",
                category="hardcoded_secret",
                path=path,
                line=line,
                message=f"检测到疑似硬编码密钥字面量（{secret_kind}）",
                suggestion="移除代码中的固定密钥，改为通过插件配置项注入",
                evidence=f"redacted hardcoded secret literal (kind={secret_kind})",
            )
        )

    decode_calls = len(call_names & _DECODE_CALL_NAMES)
    has_long_encoded = any(_is_suspicious_encoded_literal(value) for _, value in literals)
    is_critical_obfuscation = (
        decode_calls >= 2 and (has_dynamic_exec or "marshal.loads" in call_names)
    ) or (has_long_encoded and has_dynamic_exec)
    is_high_obfuscation = has_long_encoded and decode_calls >= 1
    if is_critical_obfuscation or is_high_obfuscation:
        findings.append(
            _finding(
                rule_id="PY008",
                severity="critical" if is_critical_obfuscation else "high",
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
    try:
        parsed = parse_requirements(source)
    except RequirementsParseError as exc:
        return [
            _finding(
                rule_id="REQ003",
                severity="high",
                category="dependency_manifest",
                path=path,
                line=None,
                message="requirements 文件无法安全解析",
                suggestion="使用 UTF-8 和标准 PEP 508 依赖声明",
                evidence=f"code={exc.code}",
            )
        ]

    findings: list[dict[str, Any]] = []
    for diagnostic in parsed.diagnostics:
        is_conflict = diagnostic.code == "dependency_declaration_conflict"
        is_source = diagnostic.code in {
            "dependency_direct_url",
            "dependency_vcs_source",
            "dependency_local_source",
            "dependency_editable_source",
            "dependency_source_option",
            "dependency_option_invalid",
        }
        findings.append(
            _finding(
                rule_id="REQ002" if is_conflict else ("REQ001" if is_source else "REQ003"),
                severity="medium" if is_conflict else "high",
                category=(
                    "dependency_conflict"
                    if is_conflict
                    else ("dependency_source" if is_source else "dependency_manifest")
                ),
                path=path,
                line=diagnostic.line_number or None,
                message=(
                    f"依赖 {diagnostic.name} 存在多个不一致声明"
                    if is_conflict and diagnostic.name
                    else diagnostic.message
                ),
                suggestion=(
                    "合并为单一、可解析的版本范围"
                    if is_conflict
                    else "仅使用受信任索引中的标准 PEP 508 依赖声明"
                ),
                evidence=diagnostic.evidence,
            )
        )
    return findings


def _imported_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                target = alias.name if alias.asname else local
                aliases[local] = target
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                if node.level == 0 and node.module:
                    aliases[local] = f"{node.module}.{alias.name}"
                elif node.level > 0:
                    prefix = "." * node.level + (node.module or "")
                    aliases[local] = f"{prefix}.{alias.name}"
    return aliases


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else ""
    return ""


def _resolve_call_name(node: ast.expr, aliases: dict[str, str]) -> str:
    raw = _call_name(node)
    if not raw:
        return ""
    head, *rest = raw.split(".")
    resolved_head = aliases.get(head, head)
    return ".".join([resolved_head, *rest])


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _nested_compile_call_ids(tree: ast.AST, aliases: dict[str, str]) -> set[int]:
    nested: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _resolve_call_name(node.func, aliases)
        if name not in {"eval", "exec", "builtins.eval", "builtins.exec"}:
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Call) and _resolve_call_name(first_arg.func, aliases) in {
            "compile",
            "builtins.compile",
        }:
            nested.add(id(first_arg))
    return nested


def _is_constant_module_import(node: ast.Call, name: str) -> bool:
    if name == "importlib.util.spec_from_file_location":
        return False
    if not node.args:
        return False
    first = node.args[0]
    return isinstance(first, ast.Constant) and isinstance(first.value, str) and bool(first.value)


def _is_dangerous_process_call(node: ast.Call, name: str) -> bool:
    if name in {
        "os.system",
        "os.popen",
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        "asyncio.create_subprocess_shell",
    }:
        return True
    for keyword in node.keywords:
        if (
            keyword.arg == "shell"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
        ):
            return True
    if not node.args:
        return True
    cmd = node.args[0]
    if isinstance(cmd, (ast.List, ast.Tuple)) and cmd.elts:
        first_elem = cmd.elts[0]
        if not (isinstance(first_elem, ast.Constant) and isinstance(first_elem.value, str)):
            return True
        program = PurePosixPath(first_elem.value.replace("\\", "/")).name.casefold()
        return program in _SHELL_OR_INTERPRETER_BINS
    return True


def _executable_string_literals(
    tree: ast.AST,
    docstring_ids: set[int],
) -> list[tuple[int | None, str]]:
    literals: list[tuple[int | None, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_ids
        ):
            literals.append((getattr(node, "lineno", None), node.value))
    return literals


def _first_sensitive_path_literal(
    literals: list[tuple[int | None, str]],
) -> tuple[int | None, str] | None:
    for line, value in literals:
        candidate = value.strip()
        if not candidate or len(candidate) > 512:
            continue
        if any(pattern.search(candidate) for pattern in _SENSITIVE_PATH_PATTERNS):
            return line, candidate
    return None


def _first_hardcoded_secret_literal(
    literals: list[tuple[int | None, str]],
) -> tuple[int | None, str] | None:
    for line, value in literals:
        if not value:
            continue
        for kind, pattern in _HARDCODED_SECRET_PATTERNS:
            match = pattern.search(value)
            if not match:
                continue
            if kind == "api_secret_key" and _is_placeholder_secret(match.group(0)):
                continue
            return line, kind
    return None


def _is_placeholder_secret(value: str) -> bool:
    body = value.removeprefix("sk-").lower()
    if not body:
        return True
    if len(set(body)) <= 3:
        return True
    return body.startswith(("test", "fake", "dummy", "example", "placeholder", "your"))


def _is_suspicious_encoded_literal(value: str) -> bool:
    stripped = value.strip()
    if len(stripped) < 512 or _DATA_URI_PREFIX.match(stripped):
        return False
    return bool(LONG_ENCODED_PATTERN.search(stripped))


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
