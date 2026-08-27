"""Stage 8 -- outreach: write the first message.

Everything before this produces a row in a spreadsheet. This turns the row into
something a person can send, and it is the only stage whose output is judged by
whether a stranger replies.

The whole trick is that Judge already found a specific, checkable fact about
each company -- "Australian subsidiary, Dandenong South VIC", "supplied 40
units to a US mining customer". A message that opens on that fact could only
have been written to that recipient, which is exactly why it gets answered. A
message that opens on "I hope this finds you well" could have been sent to
anybody, which is exactly why it does not.

So the opener is required to come from `qualification_evidence`. If there is no
evidence, there is no message -- the stage refuses rather than inventing a
reason to be in touch. That refusal is the feature: a fabricated "I read your
recent announcement" is worse than no email, because the recipient knows.

No sending happens here, deliberately. Prospector writes drafts; you send them
from your own mailbox, which at these volumes is also the thing that keeps them
out of spam.
"""

from __future__ import annotations

import json
import logging

from prospector.config import load_plan
from prospector.database import MAX_ATTEMPTS, get_connection, update, utc_now
from prospector.llm import get_client
from prospector.stages._runner import relevance_filter, run_batch, select_pending

log = logging.getLogger(__name__)

CHANNEL_RULES = {
    "email": (
        "Write a short email. Subject line under 60 characters, lower-case, no "
        "marketing words, no colons. Body under 110 words, four sentences at "
        "most. No greeting flourish, no 'I hope this finds you well', no "
        "signature block -- the sender adds their own."
    ),
    "linkedin": (
        "Write a LinkedIn connection note. Under 280 characters total, because "
        "that is the hard limit. No subject line -- return an empty string for "
        "it. One sentence of context, one question."
    ),
    "call": (
        "Write a phone opener the salesperson will read aloud before dialling. "
        "Return an empty subject. Under 70 words: one sentence naming why you "
        "are calling this specific company, then the question to ask. It must "
        "sound like speech, not like a letter."
    ),
}

PROMPT = """Write the first contact message to this company.

=== WHO IS WRITING, AND WHAT THEY OFFER ===
{sender}

=== WHO THEY ARE WRITING TO ===
Company:        {company}
What they do:   {products}
Size:           {size}
Contact:        {contact}

=== THE SPECIFIC FACT WE FOUND ABOUT THEM ===
This came from their own website or from public sources. It is the only thing
that makes this message worth sending, and it must appear in the opening
sentence, in plain language, as something you noticed.
{evidence}

=== WHY THIS COMPANY IS ON THE LIST ===
{objective}
Criteria they met: {matched}

=== FORMAT ===
{channel_rules}

=== RULES ===
- Open on the specific fact above. Not on yourself, not on your company.
- Do NOT claim to have read a press release, article, post or announcement
  unless one is quoted in the fact above. If you did not see it, you did not
  read it.
- Do NOT use: "I hope this finds you well", "reaching out", "touch base",
  "synergy", "solutions provider", "game-changer", "leverage", "circle back".
- Do NOT compliment them. Observe something, then ask something.
- End with ONE question that can be answered in a sentence. Not "would you be
  open to a 15 minute call" -- a real question about their business.
- Write the way one industry person writes to another: plain, specific, brief.
- If the fact above is too vague to build an honest opener on, set
  "usable": false and explain why in "angle".

Return JSON:
{{
  "angle": "the specific fact the opener uses, in a few words",
  "subject": "subject line, or empty string for linkedin and call",
  "body": "the message",
  "usable": true
}}"""


def _size(row) -> str:
    bits = [row["employees"], row["revenue"], row["founded"]]
    bits = [b for b in bits if b and "Not publicly available" not in str(b)]
    return " / ".join(str(b) for b in bits) or "unknown"


def _contact(row) -> str:
    try:
        people = json.loads(row["people_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        people = []
    if not people:
        return "no named contact found - write it so it works addressed to a role"
    person = people[0]
    return f"{person.get('name', '')}, {person.get('title', '')}".strip(", ")


def draft_one(row, plan: dict) -> dict:
    evidence = (row["qualification_evidence"] or "").strip()

    # No evidence, no honest reason to be in touch. Refusing here is the point.
    if not evidence or evidence.lower().startswith("no specific evidence"):
        raise ValueError(
            "no specific evidence to open on - this lead needs a human angle"
        )

    sender = (plan.get("sender_profile") or "").strip()
    if not sender:
        raise ValueError(
            "no sender profile on the plan - fill in who you are and what you "
            "offer, or every message comes out generic"
        )

    channel = plan.get("outreach_channel", "email")

    prompt = PROMPT.format(
        sender=sender[:1200],
        company=row["company_name"],
        products=row["products"] or "unknown",
        size=_size(row),
        contact=_contact(row),
        evidence=evidence[:1200],
        objective=plan.get("objective", ""),
        matched=row["qualification_matched"] or "not recorded",
        channel_rules=CHANNEL_RULES.get(channel, CHANNEL_RULES["email"]),
    )

    data = get_client("outreach").ask_json(prompt)

    if data.get("usable") is False:
        raise ValueError(str(data.get("angle") or "the AI could not find an honest angle"))

    body = str(data.get("body", "")).strip()
    if not body:
        raise ValueError("the AI returned an empty message")

    return {
        "outreach_angle": str(data.get("angle", ""))[:300],
        "outreach_subject": str(data.get("subject", "")).strip()[:200],
        "outreach_body": body[:3000],
        "outreach_channel": channel,
    }


def pending_sql(plan: dict | None = None) -> str:
    plan = plan or load_plan()
    min_rel = plan.get("min_relevance_for_research", "Medium")
    return (
        "SELECT * FROM leads WHERE outreach_at IS NULL "
        "AND qualification_level IS NOT NULL "
        f"AND {relevance_filter(min_rel)} "
        f"AND COALESCE(outreach_attempts, 0) < {MAX_ATTEMPTS} "
        # Best-qualified first, so a part-finished run still drafted the ones
        # actually worth sending.
        "ORDER BY COALESCE(qualification_score, 0) DESC, company_key"
    )


def run_outreach(workers: int = 3, limit: int | None = None) -> dict:
    plan = load_plan()
    rows = select_pending(pending_sql(plan), limit=limit)

    def handler(row):
        fields = draft_one(row, plan)
        update(row["company_key"], conn=get_connection(),
               outreach_at=utc_now(), outreach_error=None, **fields)
        return fields["outreach_angle"][:40]

    return run_batch("outreach", rows, handler, workers=min(workers, 4),
                     label="Writing first messages")
