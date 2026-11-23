"""Lernkarte anzeigen: Mermaid-Mindmap (Open WebUI rendert sie) + Liste der Schwächen."""

from __future__ import annotations

import re

from .model import STATUS_ICON, STATUS_LABEL, STATUS_ORDER, LearnerStore, Node

_UNSAFE = re.compile(r"[()\[\]{}\"`<>#;:|]")
LEGEND = " · ".join(f"{STATUS_ICON[s]} {STATUS_LABEL[s]}" for s in
                    ["solid", "probably", "shaky", "missing", "misconception", "untested"])


def _label(n: Node) -> str:
    title = _UNSAFE.sub(" ", re.sub(r"\s+", " ", n.title)).strip()[:60]
    return f"{STATUS_ICON[n.status]} {title}"


def _has_evidence(n: Node) -> bool:
    return bool(n.evidence) or any(_has_evidence(c) for c in n.children)


def mindmap(course_name: str, roots: list[Node], full: bool = False, max_nodes: int = 120) -> str:
    lines = ["```mermaid", "mindmap", f"  root(({_UNSAFE.sub(' ', course_name)}))"]
    count = 0

    def walk(n: Node, depth: int) -> None:
        nonlocal count
        if count >= max_nodes:
            return
        show = full or depth == 0 or _has_evidence(n)
        if not show:
            return
        count += 1
        lines.append("  " * (depth + 2) + _label(n))
        for c in n.children:
            walk(c, depth + 1)

    for r in roots:
        walk(r, 0)
    lines.append("```")
    if count >= max_nodes:
        lines.append(f"*(gekürzt auf {max_nodes} Knoten – einzelnes Thema: `/lernkarte <thema>`)*")
    return "\n".join(lines)


def weak_spots(nodes: list[Node], limit: int = 12) -> list[Node]:
    bad = [n for n in nodes if n.evidence and n.own_status in {"misconception", "missing", "shaky"}]
    bad.sort(key=lambda n: (STATUS_ORDER.index(n.own_status), -len(n.evidence)))
    return bad[:limit]


def weak_table(nodes: list[Node]) -> str:
    spots = weak_spots(nodes)
    if not spots:
        return "_Noch keine Schwächen erfasst. Jede Korrektur mit `/check` füllt die Karte._"
    rows = ["| Status | Thema | Letzter Beleg | Aufgabe |", "|---|---|---|---|"]
    for n in spots:
        last = sorted(n.evidence, key=lambda e: e.ts)[-1]
        detail = (last.detail or "").replace("|", "/").replace("\n", " ")[:140]
        rows.append(f"| {STATUS_ICON[n.own_status]} {STATUS_LABEL[n.own_status]} | {n.title} | {detail} | "
                    f"{(last.problem_ref or '').replace('|', '/')[:60]} |")
    return "\n".join(rows)


def find_subtree(roots: list[Node], query: str) -> Node | None:
    q = query.lower().strip()
    best = None

    def walk(n: Node) -> None:
        nonlocal best
        if q in n.title.lower() and (best is None or len(n.title) < len(best.title)):
            best = n
        for c in n.children:
            walk(c)

    for r in roots:
        walk(r)
    return best


def render_map(store: LearnerStore, course_id: str, course_name: str, arg: str = "") -> str:
    roots = store.tree(course_id)
    nodes = store.nodes(course_id)
    if not roots:
        return (f"Für **{course_name}** gibt es noch keine Lernkarte. Sie entsteht aus dem Skript und füllt "
                "sich mit jeder Korrektur (`/check …`).")
    arg = arg.strip()
    full = arg.lower() in {"voll", "full", "alles", "all"}
    title = f"## Lernkarte – {course_name}"
    if arg and not full:
        sub = find_subtree(roots, arg)
        if sub is None:
            return f"Kein Thema „{arg}“ in der Lernkarte gefunden."
        roots = [sub]
        title += f" › {sub.title}"
        full = True
    tested = sum(1 for n in nodes if n.evidence)
    counts = {}
    for n in nodes:
        if n.evidence:
            counts[n.own_status] = counts.get(n.own_status, 0) + 1
    summary = ", ".join(f"{STATUS_ICON[s]} {counts[s]} {STATUS_LABEL[s]}" for s in STATUS_ORDER if counts.get(s))
    parts = [title, f"{len(nodes)} Themen, davon {tested} schon geprüft" + (f": {summary}" if summary else "."),
             mindmap(course_name, roots, full=full), LEGEND, "### Das solltest du noch lernen", weak_table(nodes)]
    if not full:
        parts.append("*Ganze Karte: `/lernkarte voll` · ein Thema: `/lernkarte <thema>`*")
    return "\n\n".join(parts)
