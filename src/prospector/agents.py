"""The agent registry.

Each pipeline stage is an agent: a named worker with its own role, its own
model tier, its own concurrency and its own token budget. Putting them in one
table rather than scattering the settings through eight modules buys three
things:

  * routing becomes a visible, editable decision instead of a buried env var --
    the app can show the user exactly which agent runs on this PC and which
    goes to the cloud, and let them change it
  * the cheap high-volume agents and the expensive careful ones stop sharing a
    model, which is most of the cost saving
  * concurrency is tuned per agent. `qualify` issues six web searches per
    company and gets rate limited at eight workers; `classify` is pure text and
    is happy at eight.

Two tiers:

  bulk       short structured extraction, run hundreds of times. Small local
             model on the CPU is fine, and free.
  reasoning  judgement calls made a handful of times, where being wrong costs
             the user a real meeting. Worth the cloud model.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict

BULK = "bulk"
REASONING = "reasoning"


@dataclass
class Agent:
    name: str
    friendly: str            # shown in the app
    desc: str                # one line, plain language
    tier: str                # BULK | REASONING
    role: str                # system prompt
    workers: int = 4
    max_tokens: int = 1200
    temperature: float = 0.0
    uses_ai: bool = True
    uses_search: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


AGENTS: dict[str, Agent] = {
    "planner": Agent(
        name="planner",
        friendly="Planner",
        desc="Turns your prompt into a research plan",
        tier=REASONING,
        role="You design lead-generation research plans. You turn a "
             "salesperson's description of who they want to find into a "
             "concrete, searchable plan. You are specific and literal: vague "
             "search queries return nothing useful. You answer only with JSON.",
        workers=1, max_tokens=2500, temperature=0.3,
    ),
    "discover": Agent(
        name="discover",
        friendly="Scout",
        desc="Finds companies matching the plan",
        tier=BULK,
        role="You extract company names from web search results. You return "
             "only real operating companies, never publications, directories, "
             "marketplaces, government bodies, associations or job boards. "
             "You answer only with JSON.",
        workers=1, max_tokens=1200, uses_search=True,
    ),
    "resolve": Agent(
        name="resolve",
        friendly="Locator",
        desc="Finds each company's official website",
        tier=BULK,
        role="",                     # pure scoring, no model call
        workers=4, uses_ai=False, uses_search=True,
    ),
    "crawl": Agent(
        name="crawl",
        friendly="Reader",
        desc="Reads the pages that carry the evidence",
        tier=BULK,
        role="",                     # pure fetching, no model call
        workers=4, uses_ai=False,
    ),
    "classify": Agent(
        name="classify",
        friendly="Sorter",
        desc="Works out what each company does and whether it fits",
        tier=BULK,
        role="You are a lead-research analyst. You classify companies against "
             "a salesperson's brief. You answer only with JSON. You never "
             "invent facts: when the evidence does not support a field, you "
             "say so.",
        workers=6, max_tokens=350,
    ),
    "qualify": Agent(
        name="qualify",
        friendly="Judge",
        desc="Decides whether a company actually meets your criteria",
        tier=REASONING,
        role="You are a due-diligence researcher qualifying sales leads. You "
             "are sceptical of marketing language: 'global presence' and "
             "'trusted worldwide' are NOT evidence unless a specific country, "
             "company, project or number is named. You cite only what appears "
             "in the supplied text. You answer only with JSON.",
        # Deliberately low: this agent issues six web searches per company, and
        # hammering the free search endpoint gets it blocked for the whole run.
        workers=3, max_tokens=1200, uses_search=True,
    ),
    "profile": Agent(
        name="profile",
        friendly="Analyst",
        desc="Collects revenue, size, sites and markets",
        tier=BULK,
        role="You are a company-research analyst. You extract only facts that "
             "appear in the supplied text. Where a fact is not present you say "
             "so explicitly. You never estimate. You answer only with JSON.",
        workers=3, max_tokens=700, uses_search=True,
    ),
    "people": Agent(
        name="people",
        friendly="Connector",
        desc="Finds who to contact at each company",
        tier=BULK,
        role="You extract named people and their job titles from supplied "
             "text. You only return people who are explicitly named in the "
             "text. You never invent a person, a title or a LinkedIn URL. "
             "You answer only with JSON.",
        workers=3, max_tokens=900, uses_search=True,
    ),
    "outreach": Agent(
        name="outreach",
        friendly="Drafter",
        desc="Writes a first message built from what was actually found",
        tier=REASONING,
        role="You write short first-contact messages for a salesperson. You "
             "open with a specific fact about the recipient's own company that "
             "proves the message was written for them. You never flatter, never "
             "use filler like 'I hope this finds you well', and never claim to "
             "have read something you were not shown. You ask one question that "
             "is easy to answer. You answer only with JSON.",
        workers=3, max_tokens=700, temperature=0.4,
    ),
    "exporter": Agent(
        name="exporter",
        friendly="Scribe",
        desc="Builds the spreadsheet",
        tier=BULK,
        role="",
        workers=1, uses_ai=False,
    ),
}

# Execution order. The pipeline walks this list.
ORDER = ("discover", "resolve", "crawl", "classify", "qualify",
         "profile", "people", "outreach", "exporter")


def get(name: str) -> Agent:
    agent = AGENTS.get(name)
    if agent is None:
        raise KeyError(f"Unknown agent: {name}")
    return agent


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _local_ready() -> bool:
    return bool(os.environ.get("LOCAL_BASE_URL") and os.environ.get("LOCAL_MODEL"))


def _cloud_ready() -> bool:
    return bool(os.environ.get("OPENROUTER_API_KEY"))


def route(name: str) -> str:
    """Return "local" or "cloud" for an agent, honouring any user override.

    Reasoning agents prefer the cloud because a wrong judgement there costs a
    real meeting. Bulk agents prefer this PC because they run hundreds of times
    and the work is short structured extraction that a small model handles
    perfectly well.

    Either side missing, everything falls to whichever is configured -- the app
    stays usable with only one of the two set up.
    """
    override = os.environ.get(f"ROUTE_{name.upper()}", "").strip().lower()
    if override in ("local", "cloud"):
        if override == "local" and _local_ready():
            return "local"
        if override == "cloud" and _cloud_ready():
            return "cloud"

    agent = AGENTS.get(name)
    prefer_cloud = agent.tier == REASONING if agent else True

    if prefer_cloud:
        return "cloud" if _cloud_ready() else "local"
    return "local" if _local_ready() else "cloud"


def routing_table() -> list[dict]:
    """What the app shows on the engine screen."""
    rows = []
    anything = _local_ready() or _cloud_ready()

    for name in ("planner",) + ORDER:
        agent = AGENTS[name]
        if not agent.uses_ai:
            # Never calls a model, so it always runs here whatever is set up.
            where = "this PC"
        elif not anything:
            # With neither engine configured, route() still returns a best
            # guess for internal use -- but showing the user a confident
            # "cloud" when no key exists is a lie. Say what is true instead.
            where = "not set up"
        else:
            where = "this PC" if route(name) == "local" else "cloud"
        rows.append({
            "name": agent.name,
            "friendly": agent.friendly,
            "desc": agent.desc,
            "tier": agent.tier,
            "uses_ai": agent.uses_ai,
            "where": where,
            "overridden": bool(os.environ.get(f"ROUTE_{name.upper()}", "").strip()),
        })
    return rows


def set_override(name: str, where: str) -> None:
    """Pin one agent to local or cloud. "" clears it."""
    key = f"ROUTE_{name.upper()}"
    if where in ("local", "cloud"):
        os.environ[key] = where
    else:
        os.environ.pop(key, None)
