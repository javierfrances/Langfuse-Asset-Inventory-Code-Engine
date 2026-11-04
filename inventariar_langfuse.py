#!/usr/bin/env python3
"""Inventario incremental de recursos observados en Langfuse (API v2).

- Solo Python estándar; no envía datos a Langfuse ni modifica las trazas.
- No copia prompts, CVs, entradas o salidas al inventario.
- Añade recursos únicos; conserva los recursos ya guardados.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CATEGORIES = ("agents", "knowledge_bases", "mcp_servers", "tools", "foundation_models")
FIELDS = "core,basic,metadata,model,trace_context"
USER_AGENT = "langfuse-inventory/1.0"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_datetime(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Fecha ISO 8601 no válida: {value}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def key_norm(value: str) -> str:
    return re.sub(r"[-_\s]", "", str(value).casefold())


def flatten(data: Any, prefix: str = "", depth: int = 0) -> dict[str, Any]:
    """Indexa claves de metadatos, incluyendo propiedades anidadas."""
    result: dict[str, Any] = {}
    if depth > 6 or not isinstance(data, dict):
        return result
    for key, value in data.items():
        full = (prefix + "." if prefix else "") + str(key)
        if isinstance(value, dict):
            result.update(flatten(value, full, depth + 1))
        elif isinstance(value, (str, int, float, bool)) and value is not None:
            result[key_norm(full)] = value
    return result


def meta_value(meta: dict[str, Any], *keys: str) -> str | None:
    flat = flatten(meta)
    for key in keys:
        v = flat.get(key_norm(key))
        if isinstance(v, (str, int)) and not isinstance(v, bool):
            s = str(v).strip()
            if s and len(s) <= 200:
                return s
    return None


def structured_io_metadata(obs: dict[str, Any]) -> dict[str, Any]:
    """Lee SOLO campos estructurados explícitos; jamás almacena el I/O completo."""
    out: dict[str, Any] = {}
    accepted = {
        "agent_id", "agent_name", "tool_id", "tool_name", "tool_namespace",
        "mcp_server_id", "mcp_server_name", "mcp_server_url",
        "knowledge_base_id", "knowledge_base_name", "vector_store_id",
        "collection_name", "collection_id", "index_name", "model_name", "model_provider",
    }
    for field in ("input", "output"):
        val = obs.get(field)
        if not val:
            continue
        if isinstance(val, str):
            if len(val) > 50000:
                continue
            try:
                val = json.loads(val)
            except (ValueError, TypeError):
                continue
        if isinstance(val, dict):
            for k, v in val.items():
                if k in accepted and isinstance(v, (str, int)) and len(str(v)) <= 200:
                    out[k] = v
    return out


def stable_id(category: str, project_id: str, identity: dict[str, str]) -> str:
    raw = json.dumps([project_id, category, identity], sort_keys=True, ensure_ascii=False)
    return category[:-1] + "_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def mk_identity(*, explicit_id: str | None, name: str, service: str | None = None,
                context: str | None = None, namespace: str | None = None) -> tuple[dict[str, str], str]:
    """El ID explícito manda; el fallback incorpora contexto cuando exista."""
    if explicit_id:
        return ({"external_id": normalize(explicit_id)}, "explicit_id")
    identity = {"name": normalize(name)}
    if service:
        identity["service"] = normalize(service)
    if namespace:
        identity["namespace"] = normalize(namespace)
    if context:
        identity["context"] = normalize(context)
    return identity, "contextual_name"


def get_service(meta: dict[str, Any]) -> str | None:
    service = meta_value(meta, "service.name", "service_name", "serviceName", "deployment.name")
    service_ns = meta_value(meta, "service.namespace", "service_namespace")
    return "/".join(x for x in (service_ns, service) if x) or None


def mcp_parts(name: str, meta: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
    server_id = meta_value(meta, "mcp.server.id", "mcp_server_id", "mcpServerId")
    server_url = meta_value(meta, "mcp.server.url", "mcp_server_url", "mcpServerUrl", "mcp.endpoint")
    server_name = meta_value(meta, "mcp.server.name", "mcp_server_name", "mcpServerName", "mcp_server")
    tool_name: str | None = None
    # Sintaxis que identifica MCP explícitamente, no una tool arbitraria.
    m = re.fullmatch(r"mcp__([^_]+(?:_[^_]+)?)__(.+)", name, re.IGNORECASE)
    if m:
        server_name = server_name or m.group(1)
        tool_name = m.group(2)
    m2 = re.fullmatch(r"mcp[:/]([^:/]+)[:/](.+)", name, re.IGNORECASE)
    if m2:
        server_name = server_name or m2.group(1)
        tool_name = m2.group(2)
    return server_id, server_name, server_url, tool_name


class LangfuseAPI:
    def __init__(self, base_url: str, public_key: str, secret_key: str):
        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
        self.headers = {"Authorization": "Basic " + token, "Accept": "application/json", "User-Agent": USER_AGENT}

    def get(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        for attempt in range(3):
            request = urllib.request.Request(url, headers=self.headers)
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                body = exc.read(1000).decode("utf-8", "replace")
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    retry_str = exc.headers.get("Retry-After", "")
                    try:
                        retry = int(retry_str)
                    except ValueError:
                        retry = 2 ** attempt
                    if retry > 20:
                        raise RuntimeError(f"Langfuse HTTP {exc.code}: límite de API. Reintentar más tarde. {body}") from exc
                    time.sleep(max(1, retry))
                    continue
                raise RuntimeError(f"Langfuse HTTP {exc.code}: {body}") from exc
            except urllib.error.URLError as exc:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"Error de red al conectar con Langfuse: {exc}") from exc
        raise RuntimeError("No fue posible consultar Langfuse")

    def validate_project(self, project_id: str) -> None:
        result = self.get("/api/public/projects")
        projects = result.get("data") or []
        if project_id not in {p.get("id") for p in projects}:
            raise ValueError("LANGFUSE_PROJECT_ID no coincide con el proyecto accesible por estas API keys")

    def observations(self, from_date: datetime, to_date: datetime, limit: int = 500) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        cursor = None
        pages = 0
        seen_cursors = set()
        while True:
            params: dict[str, Any] = {
                "fromStartTime": iso(from_date), "toStartTime": iso(to_date),
                "fields": FIELDS, "limit": limit,
            }
            if cursor:
                params["cursor"] = cursor
            page = self.get("/api/public/v2/observations", params)
            data = page.get("data")
            if not isinstance(data, list):
                raise RuntimeError("Respuesta inesperada: no contiene una lista 'data'")
            observations.extend(data)
            pages += 1
            cursor = (page.get("meta") or {}).get("cursor")
            print(f"  Página {pages}: {len(data)} observaciones, acumuladas {len(observations)}", file=sys.stderr)
            if not cursor:
                break
            if cursor in seen_cursors:
                raise RuntimeError("La API ha repetido un cursor de paginación")
            seen_cursors.add(cursor)
        return observations


def initial_catalog(base_url: str, project_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": {"platform": "Langfuse", "base_url": base_url, "project_id": project_id},
        "inventory": {category: [] for category in CATEGORIES},
        "last_scan": None,
    }


def load_catalog(path: Path, base_url: str, project_id: str) -> dict[str, Any]:
    if not path.exists():
        return initial_catalog(base_url, project_id)
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if catalog.get("schema_version") != 1:
        raise ValueError("Esquema del inventario no reconocido; no se sobrescribirá")
    source = catalog.get("source") or {}
    if source.get("base_url") != base_url or source.get("project_id") != project_id:
        raise ValueError("El fichero contiene otro proyecto o URL; utiliza --output distinto")
    if not isinstance(catalog.get("inventory"), dict):
        raise ValueError("El inventario existente no tiene el formato correcto")
    for category in CATEGORIES:
        catalog["inventory"].setdefault(category, [])
        if not isinstance(catalog["inventory"][category], list):
            raise ValueError(f"Categoría no válida: {category}")
    return catalog


def upsert(catalog: dict[str, Any], category: str, project: str, name: str,
           identity: dict[str, str], method: str, observation: dict[str, Any],
           attributes: dict[str, Any] | None = None, relationship: tuple[str, str] | None = None) -> tuple[str, bool]:
    uid = stable_id(category, project, identity)
    items = catalog["inventory"][category]
    item = next((x for x in items if x.get("id") == uid), None)
    if item is None:
        item = {
            "id": uid, "name": name, "identity": identity,
            "identity_method": method,
            "first_seen": {
                "trace_id": observation.get("traceId"),
                "observation_id": observation.get("id"),
                "timestamp": observation.get("startTime"),
            },
            "attributes": {k: v for k, v in (attributes or {}).items() if v is not None},
            "relationships": {"agent_ids": [], "tool_ids": [], "mcp_server_ids": [], "knowledge_base_ids": [], "model_ids": []},
        }
        items.append(item)
        added = True
    else:
        added = False
    if relationship:
        key, related_id = relationship
        if related_id and key in item["relationships"] and related_id not in item["relationships"][key]:
            item["relationships"][key].append(related_id)
    return uid, added


def add_relation(catalog: dict[str, Any], category: str, uid: str, key: str, related_id: str | None) -> None:
    if not related_id:
        return
    for item in catalog["inventory"][category]:
        if item["id"] == uid:
            rel = item.setdefault("relationships", {}).setdefault(key, [])
            if related_id not in rel:
                rel.append(related_id)
            return


def discover(catalog: dict[str, Any], observations: list[dict[str, Any]], project_id: str,
             inspect_io: bool) -> dict[str, Any]:
    new = dict.fromkeys(CATEGORIES, 0)
    obs_by_key = {(o.get("traceId"), o.get("id")): o for o in observations}
    agents_by_obs: dict[tuple[str, str], str] = {}
    agent_ids_by_trace: dict[str, set[str]] = defaultdict(set)

    def metadata(o: dict[str, Any]) -> dict[str, Any]:
        meta = o.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (ValueError, TypeError):
                meta = {}
        result = dict(meta) if isinstance(meta, dict) else {}
        if inspect_io:
            result.update(structured_io_metadata(o))
        return result

    # Primera pasada: agentes, para recuperar su identidad desde los parentObservationId.
    for o in observations:
        if o.get("type", "").upper() != "AGENT":
            continue
        meta = metadata(o)
        name = meta_value(meta, "gen_ai.agent.name", "agent_name", "agent.name") or (o.get("name") or "").strip()
        if not name:
            continue
        external_id = meta_value(meta, "gen_ai.agent.id", "agent_id", "agent.id", "agentId")
        service = get_service(meta)
        identity, method = mk_identity(explicit_id=external_id, name=name, service=service)
        uid, added = upsert(catalog, "agents", project_id, name, identity, method, o,
                            {"external_id": external_id, "service": service})
        new["agents"] += added
        agents_by_obs[(o.get("traceId"), o.get("id"))] = uid
        agent_ids_by_trace[o.get("traceId")].add(uid)

    def parent_agent(o: dict[str, Any]) -> str | None:
        trace = o.get("traceId")
        parent_id = o.get("parentObservationId")
        visited = set()
        while parent_id and parent_id not in visited:
            visited.add(parent_id)
            key = (trace, parent_id)
            if key in agents_by_obs:
                return agents_by_obs[key]
            parent_o = obs_by_key.get(key)
            parent_id = parent_o.get("parentObservationId") if parent_o else None
        known = agent_ids_by_trace.get(trace) or set()
        return next(iter(known)) if len(known) == 1 else None

    unresolved_retrievers = 0
    for o in observations:
        typ = (o.get("type") or "").upper()
        meta = metadata(o)
        name = (o.get("name") or "").strip()
        service = get_service(meta)
        agent_id = parent_agent(o)
        mcp_id, mcp_name, mcp_url, mcp_tool_name = mcp_parts(name, meta)
        mcp_uid = None
        if mcp_name or mcp_id or mcp_url:
            mcp_title = mcp_name or mcp_url or mcp_id
            identity, method = mk_identity(explicit_id=mcp_id or mcp_url, name=mcp_title, service=service)
            mcp_uid, added = upsert(catalog, "mcp_servers", project_id, mcp_title, identity, method, o,
                                    {"external_id": mcp_id, "endpoint": mcp_url, "service": service},
                                    ("agent_ids", agent_id) if agent_id else None)
            new["mcp_servers"] += added
            if agent_id:
                add_relation(catalog, "agents", agent_id, "mcp_server_ids", mcp_uid)

        if typ == "TOOL":
            tool_name = meta_value(meta, "gen_ai.tool.name", "tool_name", "tool.name") or mcp_tool_name or name
            if tool_name:
                external_id = meta_value(meta, "tool_id", "tool.id", "gen_ai.tool.id", "mcp.tool.id")
                namespace = meta_value(meta, "tool.namespace", "tool_namespace", "gen_ai.tool.type")
                identity, method = mk_identity(
                    explicit_id=external_id, name=tool_name, service=service,
                    namespace=namespace, context=mcp_uid or agent_id,
                )
                uid, added = upsert(catalog, "tools", project_id, tool_name, identity, method, o,
                                    {"external_id": external_id, "namespace": namespace, "service": service},
                                    ("agent_ids", agent_id) if agent_id else None)
                new["tools"] += added
                if agent_id:
                    add_relation(catalog, "agents", agent_id, "tool_ids", uid)
                if mcp_uid:
                    add_relation(catalog, "tools", uid, "mcp_server_ids", mcp_uid)
                    add_relation(catalog, "mcp_servers", mcp_uid, "tool_ids", uid)

        # No se puede deducir una KB real de un nombre genérico como search_documents.
        if typ == "RETRIEVER" or meta_value(meta, "knowledge_base_id", "knowledge_base_name", "gen_ai.data_source.id"):
            kb_id = meta_value(meta, "gen_ai.data_source.id", "knowledge_base_id", "knowledgeBaseId",
                               "knowledge_base.id", "vector_store.id", "vector_store_id",
                               "collection_id", "index_id")
            kb_name = meta_value(meta, "knowledge_base_name", "knowledgeBaseName", "knowledge_base.name",
                                 "vector_store.name", "collection_name", "index_name")
            if kb_id or kb_name:
                title = kb_name or kb_id
                kb_namespace = meta_value(meta, "database.name", "db.system", "vector_store.provider")
                identity, method = mk_identity(explicit_id=kb_id, name=title, service=service, namespace=kb_namespace)
                kb_uid, added = upsert(catalog, "knowledge_bases", project_id, title, identity, method, o,
                                       {"external_id": kb_id, "service": service, "namespace": kb_namespace},
                                       ("agent_ids", agent_id) if agent_id else None)
                new["knowledge_bases"] += added
                if agent_id:
                    add_relation(catalog, "agents", agent_id, "knowledge_base_ids", kb_uid)
            elif typ == "RETRIEVER":
                unresolved_retrievers += 1

        if typ in ("GENERATION", "EMBEDDING"):
            model_name = (o.get("model") or "").strip() or meta_value(meta,
                "gen_ai.response.model", "gen_ai.request.model", "model_name")
            if model_name:
                provider = meta_value(meta, "gen_ai.provider.name", "model_provider", "llm.provider", "provider")
                identity = {"model": normalize(model_name)}
                if provider:
                    identity["provider"] = normalize(provider)
                uid, added = upsert(catalog, "foundation_models", project_id, model_name, identity,
                                    "model_field_or_metadata", o, {"provider": provider},
                                    ("agent_ids", agent_id) if agent_id else None)
                new["foundation_models"] += added
                if agent_id:
                    add_relation(catalog, "agents", agent_id, "model_ids", uid)

    for category in CATEGORIES:
        catalog["inventory"][category].sort(key=lambda x: (normalize(x.get("name")), x["id"]))
        for item in catalog["inventory"][category]:
            for relations in item.get("relationships", {}).values():
                relations.sort()
    return {"added": new, "unidentified_retriever_observations": unresolved_retrievers}


def save_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".langfuse_inventory_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    ap = argparse.ArgumentParser(description="Inventario incremental de recursos observados en Langfuse")
    ap.add_argument("--output", default="inventario_langfuse.json", help="JSON persistente de inventario")
    ap.add_argument("--days", type=int, default=30, help="Días hacia atrás (por defecto 30)")
    ap.add_argument("--from", dest="from_date", help="Inicio UTC ISO 8601, ej. 2026-10-01")
    ap.add_argument("--to", dest="to_date", help="Fin UTC ISO 8601 exclusivo, ej. 2026-11-05")
    ap.add_argument("--page-size", type=int, default=500, help="Observaciones por página (máx. 1000)")
    ap.add_argument("--inspect-io", action="store_true", help="Inspecciona claves estructuradas en input/output sin guardarlos")
    ap.add_argument("--dry-run", action="store_true", help="Muestra los cambios sin guardar")
    args = ap.parse_args()

    base = (os.environ.get("LANGFUSE_BASE_URL") or "").strip().rstrip("/")
    if base.endswith("/api/public"):
        base = base[:-len("/api/public")]
    project = os.environ.get("LANGFUSE_PROJECT_ID", "").strip()
    public = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    missing = [name for name, value in (("LANGFUSE_BASE_URL", base), ("LANGFUSE_PROJECT_ID", project),
                 ("LANGFUSE_PUBLIC_KEY", public), ("LANGFUSE_SECRET_KEY", secret)) if not value]
    if missing:
        raise ValueError("Faltan variables de entorno: " + ", ".join(missing))
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")):
        raise ValueError("LANGFUSE_BASE_URL debe utilizar HTTPS (salvo localhost)")
    if args.days <= 0 or not 1 <= args.page_size <= 1000:
        raise ValueError("--days debe ser positivo y --page-size debe estar entre 1 y 1000")
    end = parse_datetime(args.to_date) if args.to_date else utc_now()
    start = parse_datetime(args.from_date) if args.from_date else end - timedelta(days=args.days)
    if start >= end:
        raise ValueError("La fecha de inicio debe ser anterior al fin")

    output = Path(args.output).expanduser().resolve()
    catalog = load_catalog(output, base, project)
    api = LangfuseAPI(base, public, secret)
    print(f"Validando proyecto {project}...", file=sys.stderr)
    api.validate_project(project)
    print(f"Leyendo observaciones entre {iso(start)} y {iso(end)}...", file=sys.stderr)
    observations = api.observations(start, end, args.page_size)
    invalid = [o for o in observations if o.get("projectId") != project]
    if invalid:
        raise RuntimeError("Se detectaron observaciones de otro proyecto. No se guarda el inventario")
    report = discover(catalog, observations, project, args.inspect_io)
    catalog["last_scan"] = {
        "scanned_at": iso(utc_now()), "from_start_time": iso(start), "to_start_time": iso(end),
        "observations_read": len(observations), "unique_traces_read": len({o.get('traceId') for o in observations}),
        "unidentified_retriever_observations": report["unidentified_retriever_observations"],
    }
    print("\nRESULTADO DEL INVENTARIO")
    for category in CATEGORIES:
        print(f"  {category:19} total={len(catalog['inventory'][category]):4d}, nuevos={report['added'][category]:3d}")
    print(f"Observaciones leídas: {len(observations)}")
    if args.dry_run:
        print("Modo --dry-run: inventario no guardado")
    else:
        save_atomic(output, catalog)
        print(f"Inventario guardado en: {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
