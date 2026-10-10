"""Bounded structural projection of the pinned LangGraph Graph API."""

import hashlib
import inspect
import json

from .workflow_runtime import AgentStep, GraphProgram


def symbol(fn):
    return f"{fn.__module__}.{fn.__qualname__}"


def source_ref(fn):
    module = fn.__module__
    return {"path": module.replace(".", "/") + ".py", "symbol": symbol(fn)}


def binding(node):
    runnable = node.runnable
    return getattr(runnable, "func", None) or getattr(runnable, "afunc", None) or runnable


def export_program(program, environment_id):
    if not isinstance(program, GraphProgram):
        raise ValueError("unsupported executable program")
    graphs, diagnostics = [], []

    def project(current, graph_id, ancestors):
        if id(current) in ancestors or len(graphs) >= 32:
            raise ValueError("subgraph bound exceeded")
        builder = current.builder
        if len(builder.nodes) > 256:
            raise ValueError("node bound exceeded")
        unsupported = set()
        for name, branches in builder.branches.items():
            for branch in branches.values():
                if not branch.ends:
                    unsupported.add(name)
                    diagnostics.append({"code": "unbounded_route", "graph": graph_id, "node": name})
        topology = current.compiled.get_graph()
        if len(topology.nodes) > 256 or len(topology.edges) > 1024:
            raise ValueError("topology bound exceeded")
        nodes = []
        graph = {"id": graph_id, "nodes": nodes, "edges": []}
        graphs.append(graph)
        for name in sorted(topology.nodes):
            if name not in builder.nodes:
                nodes.append({"id": name, "symbol": name, "kind": "boundary"})
                continue
            fn = binding(builder.nodes[name])
            # LangGraph wraps callable objects' bound __call__ methods.
            step = fn if isinstance(fn, AgentStep) else getattr(fn, "__self__", None)
            if isinstance(step, AgentStep):
                nodes.append({"id": name, "symbol": symbol(step.adapter), "kind": "agent_call",
                              "actor_selector": symbol(step.actor), "operation": step.operation,
                              "source_ref": source_ref(step.adapter)})
            elif hasattr(fn, "get_graph") and hasattr(fn, "builder"):
                child_id = graph_id + "/" + name
                nodes.append({"id": name, "symbol": name, "kind": "subgraph", "subgraph": child_id})
                child = GraphProgram.__new__(GraphProgram)
                child.builder, child.compiled = fn.builder, fn
                project(child, child_id, ancestors | {id(current)})
            elif inspect.isfunction(fn) or inspect.ismethod(fn):
                branches = builder.branches.get(name, {})
                nodes.append({"id": name, "symbol": symbol(fn), "kind": "decision" if branches else "domain",
                              "source_ref": source_ref(fn)})
            else:
                raise ValueError("unsupported node binding")
        for edge in topology.edges:
            if edge.source in unsupported and edge.conditional:
                continue
            item = {"source": edge.source, "target": edge.target, "conditional": edge.conditional}
            if edge.conditional:
                routes = []
                for branch in builder.branches.get(edge.source, {}).values():
                    for key, destination in (branch.ends or {}).items():
                        if destination == edge.target:
                            fn = getattr(branch.path, "func", None) or getattr(branch.path, "afunc", None)
                            routes.append({**item, "route_key": str(key), "route_symbol": symbol(fn)})
                graph["edges"].extend(routes or [item])
            else:
                graph["edges"].append(item)
        graph["edges"].sort(key=lambda edge: json.dumps(edge, sort_keys=True))
        if len(graph["edges"]) > 1024:
            raise ValueError("edge bound exceeded")

    project(program, "root", set())
    body = {"schema_version": 1, "environment_id": environment_id, "basis": "executable_langgraph",
            "root_graph": "root", "graphs": graphs, "diagnostics": diagnostics}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > 262144:
        raise ValueError("workflow byte bound exceeded")
    body["structure_digest"] = hashlib.sha256(encoded).hexdigest()
    return {"status": "partial" if diagnostics else "available", "workflow": body}


def environment_workflow(environment_id):
    from .registry import get_environment_spec
    cls = get_environment_spec(environment_id).cls
    build = getattr(cls, "build_program", None)
    if build is None:
        return {"status": "unavailable", "reason": "executable_migration_pending"}
    try:
        return export_program(build(), environment_id)
    except Exception:
        return {"status": "invalid", "reason": "invalid_executable_program"}
