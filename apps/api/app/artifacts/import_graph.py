from __future__ import annotations

import ast
import hashlib
import json
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .diff import ArtifactManifestFile, validate_artifact_manifest
from .models import ArtifactErrorCode
from .repository import ArtifactRepository
from .storage import ArtifactStorage, ArtifactStorageError

IMPORT_GRAPH_SCHEMA_VERSION = "1"
IMPORT_GRAPH_TOOL_NAME = "python-ast-import-graph"
IMPORT_GRAPH_TOOL_VERSION = "python-ast-import-graph-v1"


@dataclass(frozen=True, slots=True)
class ImportGraphLimits:
    max_python_files: int = 2_000
    max_file_bytes: int = 512 * 1024
    max_total_bytes: int = 8 * 1024 * 1024
    max_lines_per_file: int = 5_000
    max_total_lines: int = 50_000
    max_ast_nodes_per_file: int = 100_000
    max_ast_depth: int = 200
    max_edges: int = 20_000
    max_reasons: int = 200
    max_review_paths: int = 2_000

    def __post_init__(self) -> None:
        values = (
            self.max_python_files,
            self.max_file_bytes,
            self.max_total_bytes,
            self.max_lines_per_file,
            self.max_total_lines,
            self.max_ast_nodes_per_file,
            self.max_ast_depth,
            self.max_edges,
            self.max_reasons,
            self.max_review_paths,
        )
        if any(value <= 0 for value in values):
            raise ValueError("Import graph limits must be positive")
        if self.max_total_bytes < self.max_file_bytes:
            raise ValueError("Total graph byte limit cannot be smaller than the per-file limit")
        if self.max_total_lines < self.max_lines_per_file:
            raise ValueError("Total graph line limit cannot be smaller than the per-file limit")

    def as_dict(self) -> dict[str, int]:
        return {
            "max_ast_depth": self.max_ast_depth,
            "max_ast_nodes_per_file": self.max_ast_nodes_per_file,
            "max_edges": self.max_edges,
            "max_file_bytes": self.max_file_bytes,
            "max_lines_per_file": self.max_lines_per_file,
            "max_python_files": self.max_python_files,
            "max_reasons": self.max_reasons,
            "max_review_paths": self.max_review_paths,
            "max_total_bytes": self.max_total_bytes,
            "max_total_lines": self.max_total_lines,
        }


@dataclass(frozen=True, slots=True)
class ImportGraphResult:
    edges: tuple[Mapping[str, Any], ...]
    file_states: tuple[Mapping[str, Any], ...]
    coverage: Mapping[str, Any]
    input_sha256: str
    output_sha256: str


class ImportGraphBuildError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class _ModuleIndex:
    unique: Mapping[str, ArtifactManifestFile]
    ambiguous: frozenset[str]
    module_by_file_id: Mapping[str, str]
    top_levels: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Analysis:
    edges: tuple[Mapping[str, Any], ...]
    file_reasons: Mapping[str, tuple[str, ...]]
    reasons: tuple[str, ...]
    entry_file_ids: frozenset[str]
    reachable_file_ids: frozenset[str]
    module_index: _ModuleIndex
    total_bytes: int
    total_lines: int

    @property
    def complete(self) -> bool:
        return not self.reasons


@dataclass(frozen=True, slots=True)
class _Resolution:
    scope: str
    target: ArtifactManifestFile | None
    resolved_module: str
    reason_code: str | None = None


class ImportGraphService:
    def __init__(self, limits: ImportGraphLimits | None = None) -> None:
        self.limits = limits or ImportGraphLimits()

    async def build(
        self,
        *,
        artifact: Mapping[str, Any],
        repository: ArtifactRepository,
        storage: ArtifactStorage,
        entrypoint_paths: Set[str] | Sequence[str] = (),
        forced_paths: Set[str] | Sequence[str] = (),
    ) -> ImportGraphResult:
        artifact_id = str(artifact.get("id") or "")
        tree_sha256 = str(artifact.get("tree_sha256") or "")
        try:
            current_files = validate_artifact_manifest(
                artifact,
                await repository.list_artifact_files(artifact_id),
            )
        except (ArtifactStorageError, ValueError) as exc:
            raise ImportGraphBuildError(
                "import_graph_manifest_incomplete",
                "Current artifact manifest is not complete enough for graph analysis",
            ) from exc

        diffs = await repository.list_artifact_diffs(artifact_id)
        requested_base_id = str(artifact.get("base_artifact_id") or "") or None
        base_artifact: Mapping[str, Any] | None = None
        base_files: tuple[ArtifactManifestFile, ...] = ()
        base_validation_reason: str | None = None
        if requested_base_id:
            candidate = await repository.get_artifact(requested_base_id)
            if (
                candidate is None
                or candidate.get("plugin_id") != artifact.get("plugin_id")
                or candidate.get("id") == artifact_id
            ):
                base_validation_reason = "base_graph_unavailable"
            else:
                try:
                    base_files = validate_artifact_manifest(
                        candidate,
                        await repository.list_artifact_files(requested_base_id),
                    )
                    base_artifact = candidate
                except (ArtifactStorageError, ValueError):
                    base_validation_reason = "base_graph_manifest_incomplete"

        entrypoints = {"main.py"}
        entrypoints.update(str(path) for path in entrypoint_paths if str(path))
        entrypoints.update(item.path for item in current_files if item.is_entrypoint)
        if any(item.path == "__init__.py" for item in current_files):
            entrypoints.add("__init__.py")
        forced = {str(path) for path in forced_paths if str(path)}
        forced.update(
            str(item.get("path") or "")
            for item in diffs
            if bool((item.get("stats") or {}).get("forced_review"))
        )
        current_index = _module_index(current_files)
        base_index = _module_index(base_files)
        removed_modules = frozenset(base_index.unique) - frozenset(current_index.unique)
        absolute_prefixes = _absolute_plugin_prefixes(artifact)

        current_analysis = await self._analyze(
            current_files,
            storage,
            entrypoint_paths=entrypoints,
            removed_modules=removed_modules,
            absolute_prefixes=absolute_prefixes,
        )
        diff_reasons = _validate_diff_bindings(
            artifact,
            base_artifact,
            diffs,
        )
        changed_paths, removed_paths = _changed_paths(diffs)
        base_analysis: _Analysis | None = None
        if removed_paths and base_artifact is not None:
            base_entrypoints = {"main.py"}
            base_entrypoints.update(item.path for item in base_files if item.is_entrypoint)
            if any(item.path == "__init__.py" for item in base_files):
                base_entrypoints.add("__init__.py")
            base_analysis = await self._analyze(
                base_files,
                storage,
                entrypoint_paths=base_entrypoints,
                removed_modules=frozenset(),
                absolute_prefixes=absolute_prefixes,
            )

        current_by_path = {item.path: item for item in current_files}
        current_by_id = {item.id: item for item in current_files}
        changed_ids = {
            current_by_path[path].id for path in changed_paths if path in current_by_path
        }
        reverse_ids = _reverse_closure(changed_ids, current_analysis.edges) - changed_ids
        reverse_impact_paths = sorted(
            current_by_id[file_id].path for file_id in reverse_ids if file_id in current_by_id
        )
        entry_impact_paths = sorted(
            current_by_id[file_id].path
            for file_id in reverse_ids & set(current_analysis.reachable_file_ids)
            if file_id in current_by_id
        )

        removed_impact_paths: list[str] = []
        if base_analysis is not None:
            base_by_path = {item.path: item for item in base_files}
            base_by_id = {item.id: item for item in base_files}
            base_to_current = _base_to_current_paths(diffs)
            deleted_ids = {base_by_path[path].id for path in removed_paths if path in base_by_path}
            impacted_base_ids = _reverse_closure(deleted_ids, base_analysis.edges) - deleted_ids
            removed_impact_paths = sorted(
                {
                    current_path
                    for file_id in impacted_base_ids
                    if file_id in base_by_id
                    and (
                        current_path := base_to_current.get(
                            base_by_id[file_id].path,
                            base_by_id[file_id].path,
                        )
                    )
                    in current_by_path
                }
            )

        finding_paths = await _deterministic_finding_paths(
            repository,
            artifact,
            current_by_path,
        )
        reachable_paths = sorted(
            current_by_id[file_id].path
            for file_id in current_analysis.reachable_file_ids
            if file_id in current_by_id
        )
        reasons = list(current_analysis.reasons)
        reasons.extend(diff_reasons)
        if requested_base_id and base_artifact is None:
            reasons.append(base_validation_reason or "base_graph_unavailable")
        if removed_paths and base_analysis is None:
            reasons.append(base_validation_reason or "base_graph_unavailable")
        if base_analysis is not None and not base_analysis.complete:
            reasons.extend(f"base_{reason}" for reason in base_analysis.reasons)
        reasons = _bounded_unique(reasons, self.limits.max_reasons)

        review_paths = set(reachable_paths)
        review_paths.update(path for path in changed_paths if path in current_by_path)
        review_paths.update(reverse_impact_paths)
        review_paths.update(entry_impact_paths)
        review_paths.update(removed_impact_paths)
        review_paths.update(finding_paths)
        review_paths.update(path for path in forced if path in current_by_path)
        if reasons:
            review_paths = {item.path for item in current_files if item.is_text}
        ordered_review_paths = sorted(review_paths)
        review_scope_complete = len(ordered_review_paths) <= self.limits.max_review_paths
        if not review_scope_complete:
            ordered_review_paths = ordered_review_paths[: self.limits.max_review_paths]
            reasons = _bounded_unique(
                [*reasons, "review_scope_truncated"],
                self.limits.max_reasons,
            )

        force_full_runtime = any(
            _is_metadata_path(path) or _is_requirement_path(path) or path in entrypoints
            for path in changed_paths | removed_paths
        )
        force_full_dependency = force_full_runtime
        file_states = _file_states(
            current_files,
            current_analysis,
            entrypoint_paths=entrypoints,
            tree_sha256=tree_sha256,
        )
        input_sha256 = _input_sha256(
            artifact,
            current_files,
            base_artifact=base_artifact,
            base_files=base_files,
            diffs=diffs,
            entrypoints=entrypoints,
            forced=forced,
            limits=self.limits,
        )
        coverage: dict[str, Any] = {
            "outcome": "completed",
            "stage_name": "import_graph",
            "complete": not reasons and review_scope_complete,
            "full_review_required": bool(reasons) or not review_scope_complete,
            "reasons": reasons,
            "tree_sha256": tree_sha256,
            "requested_base_artifact_id": requested_base_id,
            "base_artifact_id": str(base_artifact.get("id") or "") if base_artifact else None,
            "base_tree_sha256": str(base_artifact.get("tree_sha256") or "")
            if base_artifact
            else None,
            "entrypoints": sorted(path for path in entrypoints if path in current_by_path),
            "reachable_paths": reachable_paths[: self.limits.max_review_paths],
            "review_paths": ordered_review_paths,
            "review_scope_complete": review_scope_complete,
            "changed_paths": sorted(changed_paths),
            "removed_paths": sorted(removed_paths),
            "reverse_impact_paths": reverse_impact_paths,
            "entry_impact_paths": entry_impact_paths,
            "removed_impact_paths": removed_impact_paths,
            "finding_paths": sorted(finding_paths),
            "forced_paths": sorted(path for path in forced if path in current_by_path),
            "force_full_runtime": force_full_runtime,
            "force_full_dependency": force_full_dependency,
            "python_files": sum(_is_python(item) for item in current_files),
            "edge_count": len(current_analysis.edges),
            "local_edges": sum(bool(item.get("target_file_id")) for item in current_analysis.edges),
            "external_edges": sum(
                (item.get("metadata") or {}).get("scope") == "external"
                for item in current_analysis.edges
            ),
            "unknown_edges": sum(
                item.get("edge_type") == "unknown" for item in current_analysis.edges
            ),
            "dynamic_edges": sum(
                item.get("edge_type") == "dynamic" for item in current_analysis.edges
            ),
            "total_bytes": current_analysis.total_bytes,
            "total_lines": current_analysis.total_lines,
            "input_sha256": input_sha256,
            "tool_version": IMPORT_GRAPH_TOOL_VERSION,
        }
        output_sha256 = _output_sha256(file_states, current_analysis.edges, coverage)
        coverage["output_sha256"] = output_sha256
        try:
            saved_files, _ = await repository.replace_artifact_graph(
                artifact_id,
                tree_sha256=tree_sha256,
                files=file_states,
                edges=current_analysis.edges,
                coverage=coverage,
                base_artifact_id=str(base_artifact.get("id") or "") if base_artifact else None,
                base_tree_sha256=str(base_artifact.get("tree_sha256") or "")
                if base_artifact
                else None,
            )
        except ValueError as exc:
            code = str(exc)
            if code not in {
                ArtifactErrorCode.DIFF_TREE_CHANGED.value,
                ArtifactErrorCode.DIFF_BASE_INVALID.value,
                ArtifactErrorCode.IMPORT_GRAPH_INCOMPLETE.value,
            }:
                code = ArtifactErrorCode.DIFF_TREE_CHANGED.value
            raise ImportGraphBuildError(
                code,
                "Artifact tree or manifest changed while persisting import graph",
                retryable=code
                in {
                    ArtifactErrorCode.DIFF_TREE_CHANGED.value,
                    ArtifactErrorCode.DIFF_BASE_INVALID.value,
                },
            ) from exc
        saved_edges = await repository.list_dependency_edges(artifact_id)
        return ImportGraphResult(
            edges=tuple(saved_edges),
            file_states=tuple(saved_files),
            coverage=coverage,
            input_sha256=input_sha256,
            output_sha256=output_sha256,
        )

    async def _analyze(
        self,
        files: Sequence[ArtifactManifestFile],
        storage: ArtifactStorage,
        *,
        entrypoint_paths: Set[str],
        removed_modules: frozenset[str],
        absolute_prefixes: frozenset[str],
    ) -> _Analysis:
        python_files = tuple(item for item in files if _is_python(item))
        module_index = _module_index(python_files)
        file_reasons: dict[str, list[str]] = defaultdict(list)
        reasons: list[str] = []
        for module in sorted(module_index.ambiguous):
            reason = f"ambiguous_module:{module}"
            reasons.append(reason)
            for item in python_files:
                if module_index.module_by_file_id.get(item.id) == module:
                    file_reasons[item.path].append(reason)
        for item in python_files:
            if item.id not in module_index.module_by_file_id:
                reason = f"invalid_module_path:{item.path}"
                reasons.append(reason)
                file_reasons[item.path].append(reason)

        by_path = {item.path: item for item in files}
        entry_ids: set[str] = set()
        for path in sorted(entrypoint_paths):
            item = by_path.get(path)
            if item is None:
                reasons.append(f"entrypoint_missing:{path}")
            elif not _is_python(item):
                reason = f"entrypoint_not_python:{path}"
                reasons.append(reason)
                file_reasons[path].append(reason)
            else:
                entry_ids.add(item.id)

        edges: list[dict[str, Any]] = []
        total_bytes = 0
        total_lines = 0
        for index, item in enumerate(python_files):
            if index >= self.limits.max_python_files:
                reason = f"graph_file_budget_exceeded:{item.path}"
                reasons.append(reason)
                file_reasons[item.path].append(reason)
                continue
            limit_reason = _file_limit_reason(
                item,
                total_bytes=total_bytes,
                total_lines=total_lines,
                limits=self.limits,
            )
            if limit_reason:
                reason = f"{limit_reason}:{item.path}"
                reasons.append(reason)
                file_reasons[item.path].append(reason)
                continue
            try:
                content = await storage.read_text_content(
                    str(item.content_key),
                    self.limits.max_file_bytes,
                    item.sha256,
                )
                source = content.decode("utf-8")
            except ArtifactStorageError:
                reason = f"graph_content_unavailable:{item.path}"
                reasons.append(reason)
                file_reasons[item.path].append(reason)
                continue
            except UnicodeDecodeError:
                reason = f"graph_content_invalid_utf8:{item.path}"
                reasons.append(reason)
                file_reasons[item.path].append(reason)
                continue
            total_bytes += item.size_bytes
            total_lines += int(item.line_count or 0)
            try:
                tree = ast.parse(source, filename=item.path, type_comments=True)
            except SyntaxError as exc:
                reason = f"syntax_error:{item.path}:{max(int(exc.lineno or 1), 1)}"
                reasons.append(reason)
                file_reasons[item.path].append(reason)
                continue
            except RecursionError:
                reason = f"ast_depth_exceeded:{item.path}"
                reasons.append(reason)
                file_reasons[item.path].append(reason)
                continue
            node_count, depth = _ast_size(tree)
            if node_count > self.limits.max_ast_nodes_per_file:
                reason = f"ast_node_budget_exceeded:{item.path}"
                reasons.append(reason)
                file_reasons[item.path].append(reason)
                continue
            if depth > self.limits.max_ast_depth:
                reason = f"ast_depth_exceeded:{item.path}"
                reasons.append(reason)
                file_reasons[item.path].append(reason)
                continue
            extracted, extracted_reasons = _extract_edges(
                item,
                tree,
                module_index,
                removed_modules,
                absolute_prefixes,
            )
            remaining = self.limits.max_edges - len(edges)
            if len(extracted) > remaining:
                reason = f"graph_edge_budget_exceeded:{item.path}"
                reasons.append(reason)
                file_reasons[item.path].append(reason)
                extracted = extracted[: max(remaining, 0)]
            edges.extend(extracted)
            reasons.extend(extracted_reasons)
            file_reasons[item.path].extend(extracted_reasons)

        edges = _deduplicate_edges(edges)
        reachable_ids = _forward_closure(entry_ids, edges)
        bounded_reasons = _bounded_unique(reasons, self.limits.max_reasons)
        return _Analysis(
            edges=tuple(edges),
            file_reasons={
                path: tuple(_bounded_unique(values, 20))
                for path, values in sorted(file_reasons.items())
            },
            reasons=tuple(bounded_reasons),
            entry_file_ids=frozenset(entry_ids),
            reachable_file_ids=frozenset(reachable_ids),
            module_index=module_index,
            total_bytes=total_bytes,
            total_lines=total_lines,
        )


def _module_index(files: Sequence[ArtifactManifestFile]) -> _ModuleIndex:
    grouped: dict[str, list[ArtifactManifestFile]] = defaultdict(list)
    module_by_file_id: dict[str, str] = {}
    top_levels: set[str] = set()
    for item in files:
        module = _module_name(item.path)
        if module is None:
            continue
        module_by_file_id[item.id] = module
        grouped[module].append(item)
        if module:
            top_levels.add(module.split(".", 1)[0])
    ambiguous = frozenset(module for module, values in grouped.items() if len(values) > 1)
    unique = {module: values[0] for module, values in grouped.items() if len(values) == 1}
    return _ModuleIndex(
        unique=unique,
        ambiguous=ambiguous,
        module_by_file_id=module_by_file_id,
        top_levels=frozenset(top_levels),
    )


def _module_name(path: str) -> str | None:
    parsed = PurePosixPath(path)
    if parsed.suffix.casefold() != ".py":
        return None
    parts = list(parsed.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if any(not part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


def _absolute_plugin_prefixes(artifact: Mapping[str, Any]) -> frozenset[str]:
    plugin_name = str(artifact.get("plugin_name") or "").strip()
    if not plugin_name or not plugin_name.isidentifier():
        return frozenset()
    return frozenset({f"data.plugins.{plugin_name}"})


def _strip_plugin_prefix(module: str, prefixes: Set[str]) -> str | None:
    for prefix in sorted(prefixes):
        if module == prefix:
            return ""
        marker = f"{prefix}."
        if module.startswith(marker):
            return module[len(marker) :]
    return None


def _extract_edges(
    source: ArtifactManifestFile,
    tree: ast.AST,
    index: _ModuleIndex,
    removed_modules: frozenset[str],
    absolute_prefixes: frozenset[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    edges: list[dict[str, Any]] = []
    reasons: list[str] = []
    imports = sorted(
        (node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))),
        key=lambda node: (int(getattr(node, "lineno", 0)), int(getattr(node, "col_offset", 0))),
    )
    for node in imports:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = _strip_plugin_prefix(alias.name, absolute_prefixes)
                resolution = _resolve_module(
                    local_name if local_name is not None else alias.name,
                    index,
                    removed_modules,
                    allow_local=local_name is not None,
                )
                edge_type = "import" if resolution.scope != "unknown" else "unknown"
                edges.append(
                    _edge_record(
                        source,
                        resolution,
                        target_name=alias.name,
                        edge_type=edge_type,
                        line_start=node.lineno,
                        metadata={"statement": "import", "alias": alias.asname or ""},
                    )
                )
                edges.extend(
                    _package_init_edges(
                        source,
                        resolution,
                        index,
                        line_start=node.lineno,
                    )
                )
                if resolution.reason_code:
                    reasons.append(
                        f"{resolution.reason_code}:{source.path}:{alias.name}:{node.lineno}"
                    )
        else:
            base_module = _absolute_from_module(source.path, node.module, node.level)
            if base_module is None:
                for alias in node.names:
                    target_name = "." * node.level + (node.module or "")
                    if alias.name != "*":
                        target_name = f"{target_name}:{alias.name}"
                    resolution = _Resolution(
                        "unknown",
                        None,
                        "",
                        "relative_import_unknown",
                    )
                    edges.append(
                        _edge_record(
                            source,
                            resolution,
                            target_name=target_name,
                            edge_type="unknown",
                            line_start=node.lineno,
                            metadata={"statement": "from", "relative_level": node.level},
                        )
                    )
                    reasons.append(f"relative_import_unknown:{source.path}:{node.lineno}")
                continue
            allow_local = node.level > 0
            if not allow_local:
                local_base = _strip_plugin_prefix(base_module, absolute_prefixes)
                if local_base is not None:
                    base_module = local_base
                    allow_local = True
            for alias in node.names:
                resolution = _resolve_from(
                    base_module,
                    alias.name,
                    index,
                    removed_modules,
                    allow_local=allow_local,
                )
                target_name = base_module
                if alias.name != "*":
                    target_name = f"{base_module}.{alias.name}" if base_module else alias.name
                edge_type = "from" if resolution.scope != "unknown" else "unknown"
                edges.append(
                    _edge_record(
                        source,
                        resolution,
                        target_name=target_name,
                        edge_type=edge_type,
                        line_start=node.lineno,
                        metadata={
                            "statement": "from",
                            "relative_level": node.level,
                            "imported_name": alias.name,
                            "alias": alias.asname or "",
                        },
                    )
                )
                edges.extend(
                    _package_init_edges(
                        source,
                        resolution,
                        index,
                        line_start=node.lineno,
                    )
                )
                if resolution.reason_code:
                    reasons.append(
                        f"{resolution.reason_code}:{source.path}:{target_name}:{node.lineno}"
                    )

    aliases = _import_aliases(tree)
    for call in sorted(
        (node for node in ast.walk(tree) if isinstance(node, ast.Call)),
        key=lambda node: (node.lineno, node.col_offset),
    ):
        if _is_dynamic_import_call(call, aliases):
            target_name = _constant_string(call.args[0]) if call.args else ""
            target_name = target_name or "<dynamic>"
            resolution = _Resolution("unknown", None, "", "dynamic_import")
            edges.append(
                _edge_record(
                    source,
                    resolution,
                    target_name=target_name,
                    edge_type="dynamic",
                    line_start=call.lineno,
                    confidence=0.5 if target_name != "<dynamic>" else 0,
                    metadata={"statement": "dynamic"},
                )
            )
            reasons.append(f"dynamic_import:{source.path}:{call.lineno}")
    for line in _sys_path_mutation_lines(tree, aliases):
        reasons.append(f"sys_path_mutation:{source.path}:{line}")
    return _deduplicate_edges(edges), _bounded_unique(reasons, 100)


def _resolve_from(
    base_module: str,
    imported_name: str,
    index: _ModuleIndex,
    removed_modules: frozenset[str],
    *,
    allow_local: bool,
) -> _Resolution:
    candidate = base_module
    if imported_name != "*":
        candidate = f"{base_module}.{imported_name}" if base_module else imported_name
    if not allow_local:
        return _Resolution("external", None, base_module or candidate)
    if candidate in index.unique:
        return _Resolution("local", index.unique[candidate], candidate)
    if candidate in index.ambiguous:
        return _Resolution("unknown", None, candidate, "ambiguous_import")
    if candidate in removed_modules:
        return _Resolution("unknown", None, candidate, "removed_local_import")
    if base_module in index.unique:
        return _Resolution("local", index.unique[base_module], base_module)
    if base_module in index.ambiguous:
        return _Resolution("unknown", None, base_module, "ambiguous_import")
    return _resolve_module(base_module or candidate, index, removed_modules)


def _resolve_module(
    module: str,
    index: _ModuleIndex,
    removed_modules: frozenset[str],
    *,
    allow_local: bool = True,
) -> _Resolution:
    if not allow_local:
        return _Resolution("external", None, module)
    if module in index.unique:
        return _Resolution("local", index.unique[module], module)
    if module in index.ambiguous:
        return _Resolution("unknown", None, module, "ambiguous_import")
    if module in removed_modules:
        return _Resolution("unknown", None, module, "removed_local_import")
    top_level = module.split(".", 1)[0] if module else ""
    removed_top_levels = {name.split(".", 1)[0] for name in removed_modules}
    if top_level in index.top_levels:
        return _Resolution("unknown", None, module, "local_import_unresolved")
    if top_level in removed_top_levels:
        return _Resolution("unknown", None, module, "removed_local_import")
    return _Resolution("unknown", None, module, "local_import_unresolved")


def _absolute_from_module(path: str, module: str | None, level: int) -> str | None:
    current_module = _module_name(path)
    if current_module is None:
        return None
    parts = current_module.split(".") if current_module else []
    if PurePosixPath(path).name != "__init__.py" and parts:
        parts.pop()
    if level:
        remove = level - 1
        if remove > len(parts):
            return None
        if remove:
            parts = parts[:-remove]
    elif module:
        return module
    module_parts = module.split(".") if module else []
    return ".".join([*parts, *module_parts])


def _package_init_edges(
    source: ArtifactManifestFile,
    resolution: _Resolution,
    index: _ModuleIndex,
    *,
    line_start: int,
) -> list[dict[str, Any]]:
    if resolution.scope != "local" or not resolution.resolved_module:
        return []
    parts = resolution.resolved_module.split(".")
    edges: list[dict[str, Any]] = []
    for length in range(1, len(parts)):
        package = ".".join(parts[:length])
        target = index.unique.get(package)
        if target is None or PurePosixPath(target.path).name != "__init__.py":
            continue
        if target.id == source.id:
            continue
        edges.append(
            _edge_record(
                source,
                _Resolution("local", target, package),
                target_name=package,
                edge_type="import",
                line_start=line_start,
                metadata={"statement": "implicit_package"},
            )
        )
    return edges


def _edge_record(
    source: ArtifactManifestFile,
    resolution: _Resolution,
    *,
    target_name: str,
    edge_type: str,
    line_start: int,
    metadata: Mapping[str, Any],
    confidence: float | None = None,
) -> dict[str, Any]:
    target_id = resolution.target.id if resolution.target else None
    identity = "\x00".join(
        (
            IMPORT_GRAPH_TOOL_VERSION,
            source.artifact_id,
            source.id,
            target_id or "",
            target_name,
            edge_type,
            str(line_start),
        )
    )
    return {
        "id": f"edge_{hashlib.sha256(identity.encode()).hexdigest()[:32]}",
        "source_file_id": source.id,
        "target_file_id": target_id,
        "target_name": target_name,
        "edge_type": edge_type,
        "confidence": confidence
        if confidence is not None
        else (1 if resolution.scope != "unknown" else 0),
        "line_start": line_start,
        "metadata": {
            **dict(metadata),
            "scope": resolution.scope,
            "resolved_module": resolution.resolved_module,
        },
    }


def _import_aliases(tree: ast.AST) -> dict[str, set[str]]:
    aliases = {
        "sys": {"sys"},
        "sys_path": set(),
        "importlib": {"importlib"},
        "import_module": set(),
        "dunder_import": {"__import__"},
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                if alias.name == "sys":
                    aliases["sys"].add(bound)
                if alias.name == "importlib":
                    aliases["importlib"].add(bound)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if node.module == "sys" and alias.name == "path":
                    aliases["sys_path"].add(bound)
                if node.module == "importlib" and alias.name == "import_module":
                    aliases["import_module"].add(bound)
                if node.module == "builtins" and alias.name == "__import__":
                    aliases["dunder_import"].add(bound)
    return aliases


def _is_dynamic_import_call(call: ast.Call, aliases: Mapping[str, set[str]]) -> bool:
    if isinstance(call.func, ast.Name):
        return call.func.id in aliases["dunder_import"] | aliases["import_module"]
    chain = _attribute_chain(call.func)
    return bool(
        len(chain) == 2 and chain[0] in aliases["importlib"] and chain[1] == "import_module"
    )


def _sys_path_mutation_lines(
    tree: ast.AST,
    aliases: Mapping[str, set[str]],
) -> list[int]:
    lines: set[int] = set()
    mutating_methods = {"append", "clear", "extend", "insert", "pop", "remove", "reverse"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            chain = _attribute_chain(node.func)
            if (
                len(chain) == 3
                and chain[0] in aliases["sys"]
                and chain[1] == "path"
                and chain[2] in mutating_methods
            ) or (
                len(chain) == 2 and chain[0] in aliases["sys_path"] and chain[1] in mutating_methods
            ):
                lines.add(node.lineno)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets.extend(node.targets)
            elif isinstance(node, ast.Delete):
                targets.extend(node.targets)
            else:
                targets.append(node.target)
            if any(_is_sys_path_target(target, aliases) for target in targets):
                lines.add(int(getattr(node, "lineno", 1)))
    return sorted(lines)


def _is_sys_path_target(node: ast.AST, aliases: Mapping[str, set[str]]) -> bool:
    while isinstance(node, ast.Subscript):
        node = node.value
    chain = _attribute_chain(node)
    return bool(
        (len(chain) == 2 and chain[0] in aliases["sys"] and chain[1] == "path")
        or (len(chain) == 1 and chain[0] in aliases["sys_path"])
    )


def _attribute_chain(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return tuple(reversed(parts))
    return ()


def _constant_string(node: ast.AST) -> str:
    return str(node.value) if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def _ast_size(tree: ast.AST) -> tuple[int, int]:
    count = 0
    maximum_depth = 0
    pending: list[tuple[ast.AST, int]] = [(tree, 1)]
    while pending:
        node, depth = pending.pop()
        count += 1
        maximum_depth = max(maximum_depth, depth)
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return count, maximum_depth


def _file_limit_reason(
    item: ArtifactManifestFile,
    *,
    total_bytes: int,
    total_lines: int,
    limits: ImportGraphLimits,
) -> str | None:
    if not item.is_text:
        return "graph_file_not_text"
    if item.size_bytes > limits.max_file_bytes:
        return "graph_file_too_large"
    if int(item.line_count or 0) > limits.max_lines_per_file:
        return "graph_file_too_many_lines"
    if total_bytes + item.size_bytes > limits.max_total_bytes:
        return "graph_total_bytes_exceeded"
    if total_lines + int(item.line_count or 0) > limits.max_total_lines:
        return "graph_total_lines_exceeded"
    return None


def _file_states(
    files: Sequence[ArtifactManifestFile],
    analysis: _Analysis,
    *,
    entrypoint_paths: Set[str],
    tree_sha256: str,
) -> list[dict[str, Any]]:
    edge_counts = defaultdict(int)
    for edge in analysis.edges:
        edge_counts[str(edge.get("source_file_id") or "")] += 1
    return [
        {
            "file_id": item.id,
            "is_entrypoint": item.path in entrypoint_paths,
            "is_reachable": item.id in analysis.reachable_file_ids,
            "graph_status": (
                "incomplete"
                if _is_python(item) and analysis.file_reasons.get(item.path)
                else "complete"
                if _is_python(item)
                else "not_applicable"
            ),
            "scan_summary": {
                "tool_version": IMPORT_GRAPH_TOOL_VERSION,
                "tree_sha256": tree_sha256,
                "module": analysis.module_index.module_by_file_id.get(item.id, ""),
                "edge_count": edge_counts[item.id],
                "reasons": list(analysis.file_reasons.get(item.path, ())),
            },
        }
        for item in files
    ]


def _validate_diff_bindings(
    artifact: Mapping[str, Any],
    base_artifact: Mapping[str, Any] | None,
    diffs: Sequence[Mapping[str, Any]],
) -> list[str]:
    if artifact.get("base_artifact_id") and not diffs:
        return ["diff_scope_unavailable"]
    current_tree = artifact.get("tree_sha256")
    base_id = str(base_artifact.get("id") or "") if base_artifact else None
    base_tree = base_artifact.get("tree_sha256") if base_artifact else None
    if any(
        item.get("current_tree_sha256") != current_tree
        or (str(item.get("base_artifact_id") or "") or None) != base_id
        or item.get("base_tree_sha256") != base_tree
        for item in diffs
    ):
        return ["diff_scope_stale"]
    return []


def _changed_paths(
    diffs: Sequence[Mapping[str, Any]],
) -> tuple[set[str], set[str]]:
    changed: set[str] = set()
    removed: set[str] = set()
    for item in diffs:
        change_type = str(item.get("change_type") or "")
        if change_type == "unchanged":
            continue
        if change_type == "deleted":
            path = str(item.get("base_path") or item.get("path") or "")
            if path:
                removed.add(path)
            continue
        path = str(item.get("resolved_current_path") or item.get("path") or "")
        if path:
            changed.add(path)
    return changed, removed


def _base_to_current_paths(diffs: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    return {
        base_path: current_path
        for item in diffs
        if str(item.get("change_type") or "") != "deleted"
        and (base_path := str(item.get("base_path") or ""))
        and (current_path := str(item.get("resolved_current_path") or item.get("path") or ""))
    }


async def _deterministic_finding_paths(
    repository: ArtifactRepository,
    artifact: Mapping[str, Any],
    current_by_path: Mapping[str, ArtifactManifestFile],
) -> set[str]:
    runs = await repository.list_review_runs(str(artifact["id"]))
    policy_id = str(artifact.get("policy_version_id") or "")
    valid_run_ids = {
        str(run.get("id") or "")
        for run in runs
        if not policy_id or str(run.get("policy_version_id") or "") == policy_id
    }
    findings = await repository.list_findings(str(artifact["id"]))
    return {
        path
        for item in findings
        if bool(item.get("deterministic"))
        and str(item.get("status") or "open") == "open"
        and (not valid_run_ids or str(item.get("run_id") or "") in valid_run_ids)
        and (path := str(item.get("file_path") or "")) in current_by_path
    }


def _forward_closure(
    seeds: Set[str],
    edges: Sequence[Mapping[str, Any]],
) -> set[str]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        source = str(edge.get("source_file_id") or "")
        target = str(edge.get("target_file_id") or "")
        if source and target:
            adjacency[source].add(target)
    return _closure(seeds, adjacency)


def _reverse_closure(
    seeds: Set[str],
    edges: Sequence[Mapping[str, Any]],
) -> set[str]:
    reverse: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        source = str(edge.get("source_file_id") or "")
        target = str(edge.get("target_file_id") or "")
        if source and target:
            reverse[target].add(source)
    return _closure(seeds, reverse)


def _closure(seeds: Set[str], adjacency: Mapping[str, set[str]]) -> set[str]:
    reached = set(seeds)
    pending = deque(sorted(seeds))
    while pending:
        source = pending.popleft()
        for target in sorted(adjacency.get(source, set())):
            if target not in reached:
                reached.add(target)
                pending.append(target)
    return reached


def _deduplicate_edges(edges: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in edges:
        key = (
            item.get("source_file_id"),
            item.get("target_file_id"),
            item.get("target_name"),
            item.get("edge_type"),
            item.get("line_start"),
        )
        unique.setdefault(key, dict(item))
    return sorted(
        unique.values(),
        key=lambda item: (
            str(item.get("source_file_id") or ""),
            int(item.get("line_start") or 0),
            str(item.get("target_name") or ""),
            str(item.get("id") or ""),
        ),
    )


def _is_python(item: ArtifactManifestFile) -> bool:
    return item.language == "python" or PurePosixPath(item.path).suffix.casefold() == ".py"


def _is_metadata_path(path: str) -> bool:
    return path.casefold() in {"metadata.yaml", "metadata.yml"}


def _is_requirement_path(path: str) -> bool:
    name = PurePosixPath(path).name.casefold()
    return name == "requirements.txt" or (
        name.startswith("requirements-") and name.endswith(".txt")
    )


def _input_sha256(
    artifact: Mapping[str, Any],
    current_files: Sequence[ArtifactManifestFile],
    *,
    base_artifact: Mapping[str, Any] | None,
    base_files: Sequence[ArtifactManifestFile],
    diffs: Sequence[Mapping[str, Any]],
    entrypoints: Set[str],
    forced: Set[str],
    limits: ImportGraphLimits,
) -> str:
    return _sha256_json(
        {
            "schema_version": IMPORT_GRAPH_SCHEMA_VERSION,
            "tool_version": IMPORT_GRAPH_TOOL_VERSION,
            "artifact_id": artifact.get("id"),
            "tree_sha256": artifact.get("tree_sha256"),
            "requested_base_artifact_id": artifact.get("base_artifact_id"),
            "base_artifact_id": base_artifact.get("id") if base_artifact else None,
            "base_tree_sha256": base_artifact.get("tree_sha256") if base_artifact else None,
            "current_manifest": [_graph_manifest_record(item) for item in current_files],
            "base_manifest": [_graph_manifest_record(item) for item in base_files],
            "diffs": [
                {
                    "id": item.get("id"),
                    "path": item.get("path"),
                    "base_path": item.get("base_path"),
                    "change_type": item.get("change_type"),
                    "base_sha256": item.get("base_sha256"),
                    "current_sha256": item.get("current_sha256"),
                    "base_tree_sha256": item.get("base_tree_sha256"),
                    "current_tree_sha256": item.get("current_tree_sha256"),
                }
                for item in diffs
            ],
            "entrypoints": sorted(entrypoints),
            "forced_paths": sorted(forced),
            "limits": limits.as_dict(),
        }
    )


def import_graph_output_sha256(
    files: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Any],
) -> str:
    public_coverage = {
        key: value for key, value in coverage.items() if key not in {"output_sha256"}
    }
    return _sha256_json(
        {
            "schema_version": IMPORT_GRAPH_SCHEMA_VERSION,
            "tool_version": IMPORT_GRAPH_TOOL_VERSION,
            "files": sorted(
                [
                    {
                        "file_id": item.get("file_id") or item.get("id"),
                        "is_entrypoint": bool(item.get("is_entrypoint")),
                        "is_reachable": bool(item.get("is_reachable")),
                        "graph_status": item.get("graph_status"),
                        "scan_summary": dict(item.get("scan_summary") or {}),
                    }
                    for item in files
                ],
                key=lambda item: str(item.get("file_id") or ""),
            ),
            "edges": sorted(
                [
                    {
                        "id": item.get("id"),
                        "source_file_id": item.get("source_file_id"),
                        "target_file_id": item.get("target_file_id"),
                        "target_name": item.get("target_name"),
                        "edge_type": item.get("edge_type"),
                        "confidence": float(item.get("confidence") or 0),
                        "line_start": item.get("line_start"),
                        "metadata": dict(item.get("metadata") or {}),
                    }
                    for item in edges
                ],
                key=lambda item: (
                    str(item.get("source_file_id") or ""),
                    int(item.get("line_start") or 0),
                    str(item.get("target_file_id") or ""),
                    str(item.get("target_name") or ""),
                    str(item.get("edge_type") or ""),
                    str(item.get("id") or ""),
                ),
            ),
            "coverage": public_coverage,
        }
    )


def _graph_manifest_record(item: ArtifactManifestFile) -> dict[str, Any]:
    return {
        "id": item.id,
        "is_text": item.is_text,
        "language": item.language,
        "line_count": item.line_count,
        "path": item.path,
        "sha256": item.sha256,
        "size_bytes": item.size_bytes,
    }


def _output_sha256(
    files: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Any],
) -> str:
    return import_graph_output_sha256(files, edges, coverage)


def _bounded_unique(values: Sequence[str], maximum: int) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
            if len(result) >= maximum:
                break
    return result


def _sha256_json(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
