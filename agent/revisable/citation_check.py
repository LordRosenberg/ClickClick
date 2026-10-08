"""Read-only citation experiment. No runtime hook, summary mutation or device action."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

from shared.jev_gateway import JevProviderError, PROMPT_VERSION


SOURCE = re.compile(r"(dialogue|observation|note|event|stage|measurement|compaction):([^@#]+)@([1-9][0-9]*)")
SECRET_KEY = re.compile(r"(?:password|passwd|passphrase|secret|api[_-]?key|authorization|cookie|access[_-]?token|refresh[_-]?token)", re.I)
INLINE_SECRET = re.compile(r"(?i)(?:Bearer\s+[A-Za-z0-9._~+/-]+|\bsk-[A-Za-z0-9_-]{8,}|(?:api[_-]?key|password|secret|access_token|refresh_token)\s*[:=]\s*[\"']?[^\s\"',;&}]+)")
IMAGE = re.compile(r"data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+", re.I)


def sanitize(value, *, secrets=()):
    """Copy data; remove credentials, attachments and nested serialized credentials."""
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if SECRET_KEY.search(str(k)) else
                "[image attachment omitted]" if str(k) in {"image_url", "image_ref"} else
                sanitize(v, secrets=secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, secrets=secrets) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            value = json.dumps(sanitize(parsed, secrets=secrets), ensure_ascii=False)
        value = IMAGE.sub("[image attachment omitted]", INLINE_SECRET.sub("[REDACTED]", value))
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    return value


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass
class SourcePackage:
    task_id: str
    claim: str
    requested_sources: list[str]
    records: list[dict] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    evidence_mode: str = "text"
    # Hash the complete original input, even if two versions redact to the same text.
    original_hash: str = ""

    def state(self):
        return {"claim": self.claim, "sources": self.records, "evidence_mode": self.evidence_mode}


class TaskStoreReader:
    """Read exact versions through an existing task store; never create or migrate a DB."""

    def __init__(self, store):
        self.store = store

    def __call__(self, task_id, kind, key, version):
        if task_id != self.store.task_id:
            raise ValueError("Cross-task source")
        row = self.store.get(kind, key, version)
        payload = row["payload"]
        content = (json.loads(self.store.artifacts.read_text(payload["ref"]))
                   if kind == "dialogue" else payload)
        return {"task_id": task_id, "content": content, "step": payload.get("step")}


def build_source_package(task_id: str, claim: str, sources: list[str], reader: Callable,
                         *, max_chars=60000, evidence_mode="text") -> SourcePackage:
    """reader(task_id, kind, key, version) returns exact original content and task_id."""
    package = SourcePackage(task_id, sanitize(claim), list(sources), evidence_mode=evidence_mode)
    original, visited, active = [], set(), set()

    def visit(source, depth=0):
        if source in active or depth > 16:
            package.issues.append("cyclic_or_deep_source")
            return
        if source in visited:
            return
        match = SOURCE.fullmatch(source)
        if not match:
            package.issues.append("invalid_or_partial_source")
            return
        kind, key, version = match.groups()
        try:
            row = reader(task_id, kind, unquote(key), int(version))
            if row["task_id"] != task_id:
                package.issues.append("cross_task_source")
                return
            content = row["content"]
            original.append({"source": source, "content": content,
                             **({"raw_hash": row["raw_hash"]} if "raw_hash" in row else {})})
        except (OSError, ValueError, KeyError, TypeError):
            package.issues.append("unreadable_source")
            return
        visited.add(source)
        active.add(source)
        if kind == "compaction":
            # Recover provenance only. Never send the prior summary's assertions as evidence.
            try:
                summary = content["summary"]
                summary = json.loads(summary) if isinstance(summary, str) else summary
                refs = [ref for section in ("results", "decisions_and_attempts", "critical_context")
                        for item in summary.get(section, []) for ref in item.get("sources", [])]
                if not refs:
                    package.issues.append("summary_has_no_original_provenance")
                for ref in refs:
                    visit(ref, depth + 1)
            except (ValueError, TypeError, AttributeError, KeyError):
                package.issues.append("unreadable_summary_provenance")
        else:
            record_type = {"observation": "device_observation", "note": "model_authored_note",
                           "event": "operation_record_not_proof_of_effect", "stage": "plan",
                           "measurement": "metadata_or_measurement", "dialogue": "role_labeled_dialogue"}[kind]
            if kind == "dialogue" and not isinstance(content, list):
                package.issues.append("unreadable_dialogue")
            else:
                package.records.append({"source": source, "type": record_type,
                                        "step": row.get("step"), "content": sanitize(content)})
            if kind == "note" and isinstance(content, dict):
                for ref in content.get("source_refs", []):
                    visit(ref, depth + 1)
        active.remove(source)

    if evidence_mode not in {"text", "visual", "calculation"}:
        raise ValueError("Unknown evidence mode")
    if not sources or not claim.strip():
        package.issues.append("missing_claim_or_sources")
    for source in sorted(set(sources)):
        visit(source)
    package.original_hash = digest(original)
    if evidence_mode != "text":
        package.issues.append("requires_unavailable_" + evidence_mode)
    if len(canonical(package.state())) > max_chars:
        package.issues.append("source_limit_exceeded")
    if not package.records:
        package.issues.append("no_original_evidence")
    package.issues = sorted(set(package.issues))
    return package


def matching_quote(quote: str, records: list[dict]) -> bool:
    def normalized(text):
        return re.sub(r"\s+", " ", text.translate(str.maketrans("“”‘’", "\"\"''"))).strip()
    needle = normalized(quote)
    # Match in source text only, never in source IDs, counters or audit bookkeeping.
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key not in {"source", "step", "role", "id", "tool_call_id"}:
                    yield from strings(item)
    return bool(needle) and any(needle in normalized(text) for record in records
                               for text in strings(record["content"]))


class CitationChecker:
    def __init__(self, provider, output_dir: Path, *, threshold=0.8, prompt_version=PROMPT_VERSION,
                 provider_identity="typesafe", execution_mode="live", cache_limit=1000):
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("Invalid threshold")
        if cache_limit < 1:
            raise ValueError("Cache must retain at least one entry")
        self.provider, self.threshold, self.prompt_version = provider, threshold, prompt_version
        self.provider_identity, self.execution_mode = provider_identity, execution_mode
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cache, self.cache_limit = {}, cache_limit
        self.audit_path = self.output_dir / ("audit-" + uuid.uuid4().hex + ".jsonl")

    async def check(self, package: SourcePackage, *, quote: str | None = None):
        secrets = (self.provider.config.api_key,)
        safe = sanitize(package.state(), secrets=secrets)
        key = digest({"task": package.task_id, "claim": package.claim, "sources": package.requested_sources,
                      "original_hash": package.original_hash, "state": safe, "prompt": self.prompt_version,
                      "model": self.provider.config.model, "provider": self.provider_identity,
                      "endpoint": self.provider.config.base_url, "mode": self.execution_mode})
        result = {"task_id": package.task_id, "claim": safe["claim"],
                  "sources": package.requested_sources, "source_hash": package.original_hash,
                  "prompt_version": self.prompt_version, "configured_model": self.provider.config.model,
                  "execution_mode": self.execution_mode, "cache_key": key, "cached": False,
                  "threshold": self.threshold, "issues": package.issues, "admission": "not_evaluated"}
        if package.issues:
            result["verdict"] = "indeterminate"
        elif quote is not None and not matching_quote(sanitize(quote, secrets=secrets), safe["sources"]):
            result.update(verdict="missing_quote", admission="not_evaluated")
        else:
            response = self.cache.get(key)
            result["cached"] = response is not None
            try:
                if response is None:
                    response = await self.provider.classify(safe)
                    if len(self.cache) >= self.cache_limit:
                        self.cache.pop(next(iter(self.cache)))
                    self.cache[key] = response
                result.update(verdict={"supports": "supported", "contradicts": "contradicted",
                                       "says_nothing": "unsupported"}[response.relation],
                              confidence=response.confidence, probabilities=response.probabilities,
                              actual_model=response.model, usage={"input_tokens": response.input_tokens,
                                                                  "output_tokens": response.output_tokens},
                              provider_latency_ms=response.latency_ms,
                              transport_attempts=0 if result["cached"] else response.transport_attempts,
                              admission="high_confidence_support" if response.relation == "supports" and
                              response.confidence >= self.threshold else "needs_review",
                              raw_response=sanitize(response.raw, secrets=secrets))
            except JevProviderError as exc:
                result.update(verdict="provider_error", error_category=exc.category,
                              transport_attempts=exc.transport_attempts)
        result = sanitize(result, secrets=secrets)
        with self.audit_path.open("a", encoding="utf-8") as file:
            file.write(canonical(result) + "\n")
        return result
