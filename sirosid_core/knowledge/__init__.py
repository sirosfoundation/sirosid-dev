"""What the platform knows about SIROS ID, as data an agent (and a person) can read.

Topics are Markdown files in this directory with a small YAML front matter:

    ---
    title: Lifecycle of an environment
    summary: One sentence for a topic list.
    digest: One to three lines the agent should keep in mind at ALL times (goes into the system prompt).
    order: 20
    tags: [instances, lifecycle]
    ---
    Markdown body...

`examples.yaml` holds the example prompts shown in the console and offered through MCP.

Pure and dependency-light (PyYAML only, like the rest of sirosid_core): no I/O except reading these
package files. The service, the MCP server and the assistant all go through this module, so the
knowledge is written once.
"""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

_DIR = Path(__file__).resolve().parent
_FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)
_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]*")


@dataclass(frozen=True)
class Topic:
    id: str
    title: str
    summary: str
    digest: str
    order: int
    tags: Tuple[str, ...]
    body: str

    def to_dict(self, with_body: bool = False) -> dict:
        d = {"id": self.id, "title": self.title, "summary": self.summary, "tags": list(self.tags)}
        if with_body:
            d["body"] = self.body
        return d


def _parse(path: Path) -> Topic:
    text = path.read_text(encoding="utf-8")
    m = _FRONT.match(text)
    if not m:
        raise ValueError(f"{path.name}: missing YAML front matter")
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict) or not meta.get("title") or not meta.get("summary"):
        raise ValueError(f"{path.name}: front matter needs title and summary")
    return Topic(id=path.stem, title=str(meta["title"]), summary=str(meta["summary"]).strip(),
                 digest=str(meta.get("digest", "")).strip(), order=int(meta.get("order", 100)),
                 tags=tuple(str(t) for t in (meta.get("tags") or ())), body=m.group(2).strip())


def list_topics() -> List[Topic]:
    """Every topic, in `order` then id."""
    topics = [_parse(p) for p in sorted(_DIR.glob("*.md"))]
    return sorted(topics, key=lambda t: (t.order, t.id))


def get_topic(topic_id: str) -> Optional[Topic]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", topic_id or ""):
        return None
    return next((t for t in list_topics() if t.id == topic_id), None)


def search(query: str, limit: int = 5) -> List[Tuple[Topic, str]]:
    """Naive ranked search over titles, summaries, tags and bodies: [(topic, snippet)]. Enough for a
    knowledge base of a few dozen topics; an agent gets the best few and reads them whole."""
    terms = _WORD.findall((query or "").lower())
    if not terms:
        return []
    scored = []
    for t in list_topics():
        head = f"{t.title} {t.summary} {' '.join(t.tags)}".lower()
        body = t.body.lower()
        score = sum(5 * head.count(w) + min(body.count(w), 10) for w in terms)
        if score:
            scored.append((score, t))
    out = []
    for _, t in sorted(scored, key=lambda s: (-s[0], s[1].order, s[1].id))[:max(1, limit)]:
        out.append((t, _snippet(t.body, terms)))
    return out


def _snippet(body: str, terms: List[str], width: int = 220) -> str:
    low = body.lower()
    pos = min((p for p in (low.find(w) for w in terms) if p >= 0), default=0)
    start = max(0, pos - 60)
    s = body[start:start + width].replace("\n", " ").strip()
    return ("…" if start else "") + s + ("…" if start + width < len(body) else "")


def overview() -> str:
    """A compact digest for a system prompt: what the agent must always know and which topics exist."""
    topics = list_topics()
    lines = ["What you know about SIROS ID Dev (call get_knowledge(topic) for the full text of any topic):"]
    for t in topics:
        if t.digest:
            lines.append(f"- {t.title}: {t.digest}")
    lines.append("Topics: " + ", ".join(f"{t.id} ({t.title})" for t in topics))
    return "\n".join(lines)


def examples() -> List[Dict[str, str]]:
    """Example prompts: [{id, title, prompt, category}]."""
    data = yaml.safe_load((_DIR / "examples.yaml").read_text(encoding="utf-8")) or []
    out = []
    for e in data:
        out.append({"id": str(e["id"]), "title": str(e["title"]), "prompt": str(e["prompt"]).strip(),
                    "category": str(e.get("category", "general"))})
    return out
