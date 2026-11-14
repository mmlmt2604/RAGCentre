"""Pipeline validation, resolution and fingerprints (sections 5 and 6).

``resolve_pipeline`` applies the twelve validation rules of section 5, each problem naming the
node it concerns, and, when the configuration is valid, computes every node fingerprint and the
pipeline fingerprint. Node ids never enter a fingerprint, so renaming a node changes nothing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ragcentre.chunking import get_strategy, options_with_defaults
from ragcentre.hashing import CanonicalJsonError, canonical, node_fp, pipeline_fp
from ragcentre.models.pipeline import (
    INDEX_TYPES,
    ChunkNode,
    EmbedNode,
    IndexFtsNode,
    IndexVectorNode,
    ParseNode,
    PipelineConfig,
)
from ragcentre.providers.manager import ProviderRow
from ragcentre.providers.registry import get_type

NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SOURCE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")

# Implementation versions of the node types. Bump one whenever a code change alters what that
# node type writes for the same inputs and configuration; chunk nodes use their strategy's own.
NODE_IMPL_VERSIONS: dict[str, int] = {"parse": 1, "embed": 1, "index_vector": 1, "index_fts": 1}

# Which node type each node type reads from (rule 3).
EXPECTED_INPUT: dict[str, str | None] = {
    "parse": None,
    "chunk": "parse",
    "embed": "chunk",
    "index_vector": "embed",
    "index_fts": "chunk",
}
PROVIDER_KINDS = {"embed": "embedder", "index_vector": "vector_store"}

ProviderLookup = Callable[[str], ProviderRow | None]


def _a(noun: str) -> str:
    """The noun with its indefinite article: "an embedder", "a parser"."""
    return f"{'an' if noun[:1].lower() in 'aeiou' else 'a'} {noun}"


@dataclass(frozen=True)
class Problem:
    node_id: str | None
    path: str
    message: str

    def to_json(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "path": self.path, "message": self.message}


@dataclass
class ResolvedNode:
    node_id: str
    type: str
    node_fp: str
    inputs: list[str]  # upstream node ids, in declared order
    upstream: list[str]  # upstream node fingerprints, same order
    impl_version: int
    config: dict[str, Any]  # resolved, output-affecting config (the fingerprint input)
    runtime: dict[str, Any]  # what an executor needs: provider ids, strategy, options, ...
    provider_ids: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "type": self.type,
            "node_fp": self.node_fp,
            "inputs": self.inputs,
            "upstream": self.upstream,
            "impl_version": self.impl_version,
            "config": self.config,
            "runtime": self.runtime,
            "provider_ids": self.provider_ids,
        }


@dataclass
class Resolution:
    problems: list[Problem]
    config: PipelineConfig | None = None
    raw: dict[str, Any] | None = None
    nodes: list[ResolvedNode] = field(default_factory=list)
    retrieval: dict[str, Any] = field(default_factory=dict)  # fingerprint form
    retrieval_runtime: dict[str, Any] = field(default_factory=dict)
    fingerprint: str | None = None
    provider_ids: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.problems and self.fingerprint is not None

    def node(self, node_id: str) -> ResolvedNode:
        return next(n for n in self.nodes if n.node_id == node_id)

    def to_json(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "provider_ids": self.provider_ids,
            "nodes": [n.to_json() for n in self.nodes],
            "retrieval": self.retrieval,
            "retrieval_runtime": self.retrieval_runtime,
        }


# ---- structural errors ----------------------------------------------------------------------------


def _path(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        elif part in ("parse", "chunk", "embed", "index_vector", "index_fts") and out.endswith("]"):
            continue  # discriminated-union tag in the location
        else:
            out += f".{part}" if out else str(part)
    return out


def _structural_problems(raw: Any, exc: ValidationError) -> list[Problem]:
    nodes = raw.get("nodes") if isinstance(raw, dict) else None
    problems: list[Problem] = []
    for err in exc.errors():
        loc = tuple(err.get("loc", ()))
        node_id = None
        if (
            len(loc) >= 2
            and loc[0] == "nodes"
            and isinstance(loc[1], int)
            and isinstance(nodes, list)
            and loc[1] < len(nodes)
            and isinstance(nodes[loc[1]], dict)
        ):
            candidate = nodes[loc[1]].get("id")
            node_id = candidate if isinstance(candidate, str) else None
        message = str(err.get("msg", "invalid value"))
        if err.get("type") == "union_tag_invalid":
            message = "Unknown node type; use parse, chunk, embed, index_vector or index_fts."
        elif err.get("type") == "union_tag_not_found":
            message = "Every node needs a type."
        problems.append(Problem(node_id, _path(loc), message))
    return problems


# ---- the resolver -----------------------------------------------------------------------------------


class _Resolver:
    def __init__(self, config: PipelineConfig, providers: ProviderLookup) -> None:
        self.config = config
        self.providers = providers
        self.problems: list[Problem] = []
        self.index_of = {n.id: i for i, n in enumerate(config.nodes)}
        self.by_id = {n.id: n for n in config.nodes}

    def problem(self, node_id: str | None, path: str, message: str) -> None:
        self.problems.append(Problem(node_id, path, message))

    def provider(self, node_id: str | None, name: str | None, kind: str, path: str) -> ProviderRow | None:
        label = kind.replace("_", " ")
        if not name:
            self.problem(node_id, path, f"Choose {_a(label)} provider.")
            return None
        row = self.providers(name)
        if row is None:
            self.problem(node_id, path, f'No provider named "{name}".')
            return None
        if row.kind != kind:
            self.problem(node_id, path, f'"{name}" is {_a(row.kind.replace("_", " "))}, not {_a(label)}.')
            return None
        return row

    # rules 1-4
    def check_graph(self) -> list[str] | None:
        cfg = self.config
        seen: set[str] = set()
        for i, node in enumerate(cfg.nodes):
            if not NODE_ID_RE.match(node.id):
                self.problem(
                    node.id or None,
                    f"nodes[{i}].id",
                    "Node ids use lowercase letters, digits and underscores, start with a letter, "
                    "and are at most 32 characters.",
                )
            if node.id in seen:
                self.problem(node.id, f"nodes[{i}].id", f'Node id "{node.id}" is used twice.')
            seen.add(node.id)
        parses = [n for n in cfg.nodes if n.type == "parse"]
        if len(parses) != 1:
            self.problem(None, "nodes", "Add a parse node." if not parses else "A pipeline has exactly one parse node.")
        graph_ok = True
        for i, node in enumerate(cfg.nodes):
            expected = EXPECTED_INPUT[node.type]
            for j, src in enumerate(node.inputs):
                if src not in self.by_id:
                    self.problem(node.id, f"nodes[{i}].inputs[{j}]", f'Input "{src}" does not exist.')
                    graph_ok = False
            if expected is None:
                if node.inputs:
                    self.problem(node.id, f"nodes[{i}].inputs", "A parse node takes no inputs.")
                continue
            if len(node.inputs) != 1:
                self.problem(
                    node.id, f"nodes[{i}].inputs", f"A {node.type} node takes exactly one input, a {expected} node."
                )
                continue
            up = self.by_id.get(node.inputs[0])
            if up is not None and up.type != expected:
                self.problem(
                    node.id, f"nodes[{i}].inputs", f"A {node.type} node reads from a {expected} node, not {up.type}."
                )
        order = self.topological() if graph_ok else None
        return order

    def topological(self) -> list[str] | None:
        incoming = {n.id: [s for s in n.inputs if s in self.by_id] for n in self.config.nodes}
        done: list[str] = []
        remaining = dict(incoming)
        while remaining:
            ready = [nid for nid, ins in remaining.items() if all(s in done for s in ins)]
            if not ready:
                for nid in remaining:
                    self.problem(nid, f"nodes[{self.index_of[nid]}].inputs", "The graph has a cycle through this node.")
                return None
            # keep declared order among ready nodes
            ready.sort(key=lambda nid: self.index_of[nid])
            for nid in ready:
                done.append(nid)
                del remaining[nid]
        return done

    # rules 5, 6, 12
    def check_nodes(self) -> dict[str, dict[str, Any]]:
        """Returns per-node partial resolution (provider rows, options with defaults)."""
        info: dict[str, dict[str, Any]] = {}
        for i, node in enumerate(self.config.nodes):
            entry: dict[str, Any] = {}
            info[node.id] = entry
            if isinstance(node, ParseNode):
                if not node.routes:
                    self.problem(node.id, f"nodes[{i}].routes", "Add at least one route.")
                rows = []
                for j, route in enumerate(node.routes):
                    rows.append(self.provider(node.id, route.provider, "parser", f"nodes[{i}].routes[{j}].provider"))
                    if not _json_ok(route.options):
                        self.problem(node.id, f"nodes[{i}].routes[{j}].options", "Options must be plain JSON.")
                entry["routes"] = rows
            elif isinstance(node, ChunkNode):
                strategy = get_strategy(node.strategy)
                if strategy is None:
                    self.problem(node.id, f"nodes[{i}].strategy", f'Unknown chunk strategy "{node.strategy}".')
                    continue
                if strategy.needs_provider:
                    entry["provider"] = self.provider(node.id, node.provider, "chunker", f"nodes[{i}].provider")
                elif node.provider is not None:
                    self.problem(
                        node.id, f"nodes[{i}].provider", f"The {node.strategy} strategy does not use a provider."
                    )
                try:
                    entry["options"] = options_with_defaults(strategy, node.options)
                except ValidationError as exc:
                    for err in exc.errors():
                        where = ".".join(str(p) for p in err.get("loc", ()))
                        path = f"nodes[{i}].options" + (f".{where}" if where else "")
                        self.problem(node.id, path, str(err.get("msg", "invalid option")).removeprefix("Value error, "))
                if not _json_ok(node.options):
                    self.problem(node.id, f"nodes[{i}].options", "Options must be plain JSON.")
                entry["impl_version"] = strategy.impl_version
            elif isinstance(node, EmbedNode):
                entry["provider"] = self.provider(node.id, node.provider, "embedder", f"nodes[{i}].provider")
            elif isinstance(node, IndexVectorNode):
                row = self.provider(node.id, node.provider, "vector_store", f"nodes[{i}].provider")
                entry["provider"] = row
                if row is not None:
                    spec = get_type(row.kind, row.type)
                    caps = spec.capabilities if spec else None
                    metric = node.options.metric
                    if caps is not None and metric not in caps.metrics:
                        supported = ", ".join(sorted(caps.metrics))
                        self.problem(
                            node.id,
                            f"nodes[{i}].options.metric",
                            f"{row.name} does not support the {metric} metric (supported: {supported}).",
                        )
        return info

    # rule 7
    def check_reachability(self) -> None:
        cfg = self.config
        if not any(n.type in INDEX_TYPES for n in cfg.nodes):
            self.problem(None, "nodes", "Add at least one index node (index_vector or index_fts).")
            return
        consumers: dict[str, list[str]] = {n.id: [] for n in cfg.nodes}
        for n in cfg.nodes:
            for src in n.inputs:
                if src in consumers:
                    consumers[src].append(n.id)
        memo: dict[str, bool] = {}

        def leads(nid: str, depth: int = 0) -> bool:
            if nid in memo:
                return memo[nid]
            if self.by_id[nid].type in INDEX_TYPES:
                memo[nid] = True
            elif depth > len(cfg.nodes):
                memo[nid] = False
            else:
                memo[nid] = any(leads(c, depth + 1) for c in consumers[nid])
            return memo[nid]

        for i, n in enumerate(cfg.nodes):
            if n.type not in INDEX_TYPES and not leads(n.id):
                self.problem(n.id, f"nodes[{i}]", "This node does not lead to any index node; connect it or remove it.")

    def chunk_ancestor(self, node_id: str) -> str | None:
        current = self.by_id.get(node_id)
        steps = 0
        while current is not None and current.type != "chunk" and steps <= len(self.config.nodes):
            current = self.by_id.get(current.inputs[0]) if current.inputs else None
            steps += 1
        return current.id if current is not None and current.type == "chunk" else None

    # rules 8-11
    def check_retrieval(self) -> ProviderRow | None:
        r = self.config.retrieval
        ids: set[str] = set()
        roots: set[str | None] = set()
        if not r.sources:
            self.problem(None, "retrieval.sources", "Add at least one retrieval source.")
        for i, s in enumerate(r.sources):
            path = f"retrieval.sources[{i}]"
            if not SOURCE_ID_RE.match(s.id):
                self.problem(None, f"{path}.id", "Source ids use letters, digits, '_' and '-', starting with a letter.")
            if s.id in ids:
                self.problem(None, f"{path}.id", f'Source id "{s.id}" is used twice.')
            ids.add(s.id)
            target = self.by_id.get(s.index)
            if target is None or target.type not in INDEX_TYPES:
                self.problem(None, f"{path}.index", f'"{s.index}" is not an index node in this pipeline.')
            else:
                roots.add(self.chunk_ancestor(target.id))
            if not 1 <= s.top_k <= 1000:
                self.problem(None, f"{path}.top_k", "top_k must be between 1 and 1000.")
        if len(roots) > 1:
            self.problem(
                None,
                "retrieval.sources",
                "All sources must index the same chunk node, so their results can be fused by chunk id.",
            )
        if len(r.sources) > 1 and r.fusion is None:
            self.problem(None, "retrieval.fusion", "Choose a fusion method to merge two or more sources.")
        if len(r.sources) == 1 and r.fusion is not None:
            self.problem(None, "retrieval.fusion", "Fusion needs two or more sources; remove it.")
        if r.fusion is not None:
            if r.fusion.k < 1:
                self.problem(None, "retrieval.fusion.k", "k must be at least 1.")
            for key, weight in (r.fusion.weights or {}).items():
                if key not in ids:
                    self.problem(None, f"retrieval.fusion.weights.{key}", f'"{key}" is not a source id.')
                if weight < 0:
                    self.problem(None, f"retrieval.fusion.weights.{key}", "Weights must not be negative.")
        reranker = None
        feeding = [s.top_k for s in r.sources]
        if r.rerank is not None:
            reranker = self.provider(None, r.rerank.provider, "reranker", "retrieval.rerank.provider")
            if not 1 <= r.rerank.top_n <= 1000:
                self.problem(None, "retrieval.rerank.top_n", "top_n must be between 1 and 1000.")
            feeding.append(r.rerank.top_n)
        if not 1 <= r.top_k <= 1000:
            self.problem(None, "retrieval.top_k", "top_k must be between 1 and 1000.")
        elif feeding and r.top_k > min(feeding):
            self.problem(
                None,
                "retrieval.top_k",
                f"The final top_k ({r.top_k}) cannot be larger than the smallest value feeding it ({min(feeding)}).",
            )
        if r.expand is not None and not 0 <= r.expand.neighbors <= 5:
            self.problem(None, "retrieval.expand.neighbors", "neighbors must be between 0 and 5.")
        return reranker


def _json_ok(value: Any) -> bool:
    try:
        canonical(value)
    except CanonicalJsonError:
        return False
    return True


def resolve_pipeline(raw: Any, providers: ProviderLookup, existing_names: set[str] | None = None) -> Resolution:
    """Validate a submitted pipeline and compute its fingerprints."""
    if not isinstance(raw, dict):
        return Resolution([Problem(None, "", "A pipeline configuration must be a JSON object.")])
    try:
        config = PipelineConfig.model_validate(raw)
    except ValidationError as exc:
        return Resolution(_structural_problems(raw, exc), raw=raw)

    res = _Resolver(config, providers)
    if not NAME_RE.match(config.name):
        res.problem(None, "name", "Give the pipeline a name of letters, digits, '.', '_' or '-' (at most 64).")
    elif existing_names is not None and config.name in existing_names:
        res.problem(None, "name", f'A pipeline named "{config.name}" already exists in this corpus.')
    order = res.check_graph()
    info = res.check_nodes()
    if order is not None:
        res.check_reachability()
    reranker = res.check_retrieval()
    resolution = Resolution(res.problems, config=config, raw=raw)
    if res.problems or order is None:
        return resolution

    fps: dict[str, str] = {}
    provider_ids: list[str] = []
    for node_id in order:
        node = res.by_id[node_id]
        entry = info[node_id]
        upstream = [fps[s] for s in node.inputs]
        config_fp: dict[str, Any]
        runtime: dict[str, Any]
        ids: list[str] = []
        if isinstance(node, ParseNode):
            rows: list[ProviderRow] = entry["routes"]
            config_fp = {
                "routes": [
                    {"mime": r.mime, "provider_fp": row.fingerprint, "options": r.options}
                    for r, row in zip(node.routes, rows, strict=True)
                ]
            }
            runtime = {
                "routes": [
                    {"mime": r.mime, "provider_id": row.id, "provider_fp": row.fingerprint, "options": r.options}
                    for r, row in zip(node.routes, rows, strict=True)
                ]
            }
            ids = [row.id for row in rows]
            impl = NODE_IMPL_VERSIONS["parse"]
        elif isinstance(node, ChunkNode):
            config_fp = {"strategy": node.strategy, "options": entry["options"]}
            runtime = {"strategy": node.strategy, "options": entry["options"], "provider_id": None}
            if entry.get("provider") is not None:
                config_fp["provider_fp"] = entry["provider"].fingerprint
                runtime["provider_id"] = entry["provider"].id
                ids = [entry["provider"].id]
            impl = int(entry["impl_version"])
        elif isinstance(node, EmbedNode):
            row = entry["provider"]
            config_fp = {"provider_fp": row.fingerprint}
            runtime = {"provider_id": row.id}
            ids = [row.id]
            impl = NODE_IMPL_VERSIONS["embed"]
        elif isinstance(node, IndexVectorNode):
            row = entry["provider"]
            config_fp = {"provider_fp": row.fingerprint, "metric": node.options.metric}
            runtime = {"provider_id": row.id, "metric": node.options.metric}
            ids = [row.id]
            impl = NODE_IMPL_VERSIONS["index_vector"]
        else:
            assert isinstance(node, IndexFtsNode)
            config_fp = {"tokenizer": node.options.tokenizer}
            runtime = {"tokenizer": node.options.tokenizer}
            impl = NODE_IMPL_VERSIONS["index_fts"]
        fingerprint = node_fp(node.type, impl, config_fp, upstream)
        fps[node_id] = fingerprint
        provider_ids.extend(ids)
        resolution.nodes.append(
            ResolvedNode(
                node_id=node_id,
                type=node.type,
                node_fp=fingerprint,
                inputs=list(node.inputs),
                upstream=upstream,
                impl_version=impl,
                config=config_fp,
                runtime=runtime,
                provider_ids=ids,
            )
        )

    r = config.retrieval
    fusion = None
    if r.fusion is not None:
        weights = {s.id: float((r.fusion.weights or {}).get(s.id, 1.0)) for s in r.sources}
        fusion = {"method": r.fusion.method, "k": r.fusion.k, "weights": weights}
    neighbors = r.expand.neighbors if r.expand is not None else 0
    resolution.retrieval = {
        "sources": [{"id": s.id, "index": fps[s.index], "top_k": s.top_k} for s in r.sources],
        "fusion": fusion,
        "rerank": {"provider": reranker.fingerprint, "top_n": r.rerank.top_n} if reranker and r.rerank else None,
        "expand": {"neighbors": neighbors} if neighbors else None,
        "top_k": r.top_k,
    }
    resolution.retrieval_runtime = {
        "sources": [
            {"id": s.id, "index": s.index, "index_fp": fps[s.index], "kind": res.by_id[s.index].type, "top_k": s.top_k}
            for s in r.sources
        ],
        "fusion": fusion,
        "rerank": {"provider_id": reranker.id, "top_n": r.rerank.top_n} if reranker and r.rerank else None,
        "expand": {"neighbors": neighbors},
        "top_k": r.top_k,
    }
    if reranker is not None:
        provider_ids.append(reranker.id)
    resolution.provider_ids = list(dict.fromkeys(provider_ids))
    resolution.fingerprint = pipeline_fp(list(fps.values()), resolution.retrieval)
    return resolution


def chunk_defaults(strategy: str, options: Mapping[str, Any] | None) -> dict[str, Any]:
    s = get_strategy(strategy)
    if s is None:
        return dict(options or {})
    try:
        return options_with_defaults(s, options)
    except ValidationError:
        return dict(options or {})
