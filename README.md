# Prospector

**Describe the leads you want. Get back a researched, qualified, ranked
spreadsheet with names to call.**

You type one sentence. Prospector writes a research plan, shows it to you, then
sets ten agents to work: finding companies, reading their websites, judging
them against your criteria, sizing them up, finding who to contact, and
drafting the first message to each.

The repetitive reading runs **free on your own PC** — on your graphics card if
it turns out to be faster, which the app measures rather than assumes. The two
decisions that actually matter go to the cloud, where the better model is worth
paying for.

---

## Install

**Run `ProspectorSetup.exe` and click through it.** No Python, no terminal, no
administrator rights.

To build that installer once from source:

```powershell
powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1
```

It finds a Python, makes a clean build environment, **verifies the app imports
before freezing it**, runs PyInstaller, and compiles
`dist\ProspectorSetup-0.2.0.exe` with Inno Setup (6.2-compatible directives
only). One command, start to finish.

For development, `Start Prospector.bat` runs from source instead.

---

## The four screens

**1. Say what you want.** In your own words:

> *Indian manufacturers of crushing and screening equipment that already supply
> into Australia or other major mining markets, and who to meet at each.*

**2. Check the plan.** The AI turns that into a plan you can see and edit — the
searches it will run, what counts as relevant, what makes a lead qualify and
how much each criterion is worth, who to look for. Nothing is hidden in a
prompt. Change what looks wrong before anything runs.

**3. Choose where the AI runs.** Prospector checks your PC and tells you plainly
what it can do — including how many minutes a hundred companies will take.
Press one button and it installs everything itself: no downloads to find, no
command line, nothing to configure. Add a free
[OpenRouter](https://openrouter.ai/keys) key as well and it splits the work.

**4. Press start.** Stop and resume whenever. Close the browser tab — it keeps
going.

---

## The ten agents

| Agent | Job | Where it runs |
|---|---|---|
| **Planner** | Turns your prompt into a research plan | cloud |
| **Scout** | Finds companies matching the plan | this PC |
| **Locator** | Finds each company's official website | this PC (no AI) |
| **Reader** | Reads the pages that carry the evidence | this PC (no AI) |
| **Sorter** | Works out what each company does and whether it fits | this PC |
| **Judge** | Decides whether a company actually meets your criteria | cloud |
| **Analyst** | Collects revenue, size, sites and markets | this PC |
| **Connector** | Finds who to contact | this PC |
| **Drafter** | Writes the first message from what Judge actually found | cloud |
| **Scribe** | Builds the spreadsheet | this PC (no AI) |

Each agent has its own role, its own model tier, its own worker count and its
own token budget, all in one table (`agents.py`). That is what makes the split
possible: **Judge** and **Planner** run a handful of times and a wrong answer
costs you a real meeting, so they get the good model. **Sorter** runs on every
company and is short structured extraction, so a small local model handles it
for nothing.

You can pin any agent to either side from the AI screen. With only one engine
set up, everything falls to that one and the app still works.

**Cloud spend is capped and shown.** The plan carries a spending limit. The run
card shows what has been spent as it goes, and on reaching the limit the run
pauses rather than stopping dead — everything found is saved, and pressing
Continue after raising it picks up exactly where it left off.

**Worker counts are per agent, not global.** Judge issues six web searches per
company and gets the free search endpoint to block the whole run at eight
workers, so it is capped at three. Sorter is pure text and runs at six.

---

## Your graphics card, if it helps

The processor does the work by default. If you have a card with at least 3.5GB
of its own memory, setup spends about forty seconds finding out whether using it
is actually faster — running the same prompt both ways and comparing — and keeps
it only if it wins by a clear margin.

Nothing is assumed. A card can enumerate and turn out to be a software
renderer; an old driver can load and produce nothing; a remote-desktop session
reports an adapter that cannot do this at all. Measuring is the only way to tell,
and being wrong costs you a run that is slower than not trying.

Only one graphics backend is supported, and that is the point: it ships as a
single self-contained download and runs on NVIDIA, AMD and Intel using the
display driver you already have. The alternatives need a second runtime install
matched to your driver, and picking wrong produces an app that appears to hang.

If your card is too small, or turns out slower, or its driver fails, the app
says which in plain language and carries on with the processor.

---

## Three guards against invented evidence

A made-up Australian distributor is worse than no answer at all, so:

- the model may only cite text it was actually shown, never prior knowledge
- a country appearing in a website language-picker is explicitly **not** evidence
- a top rating returned with an **empty** evidence list is **downgraded in code**

There is a fourth, subtler one. An empty or unparseable rating maps to
*Unclear*, never to the best rating — without that guard the substring matching
underneath promotes every failed AI call to a top lead.

---

## The spreadsheet

Eight tabs, in the order you use them:

1. **Read Me** — what you asked for, how it was interpreted, coverage, caveats
2. **Call List** — ranked. Score blends the rating with your criteria weights, plus a bump for having a named contact
3. **Qualification** — every rating with its evidence and sources
4. **Company Profiles** — size and shape
5. **Contacts** — who to ask for
6. **First Contact** — a drafted opener per lead, built on the specific evidence
7. **All Leads** — everything found, filters on
8. **Summary** — live formulas, so re-rating a row updates the counts

---

## The first message

Judge finds a specific, checkable fact about each company. Drafter opens on it.

> Generic: *"I hope this finds you well. We supply crushing equipment..."*
>
> Evidence-led: *"I saw DOZCO runs its own Australian arm out of Dandenong South —
> is parts lead time from India a constraint for your customers there?"*

The second gets replied to because it could only have been written to that
recipient. Fill in **who you are and what you offer** on the plan — without it
every draft is boilerplate, and Drafter refuses rather than producing some.

It also refuses when Judge found no specific evidence. That refusal is the
feature: a fabricated *"I read your recent announcement"* is worse than sending
nothing, because the recipient knows.

Email, LinkedIn note or phone opener — pick the channel on the plan. Each has
its own length and tone rules, and a banned-phrase list that keeps drafts from
sounding like everyone else's outreach.

**Nothing is sent.** Prospector writes drafts; you send them from your own
mailbox. At these volumes — tens of messages, not thousands — that is also what
keeps them out of spam. Cold-blasting from a fresh domain burns it in a week,
and no amount of good code prevents that.

---

## Command line (optional)

The app covers all of this. Here for scripting and for debugging one agent.

```bash
prospector                                  # open the app
prospector plan "UK cleaning contractors with 50+ staff"
prospector ai                               # what can this PC do, and who runs where
prospector ai --local                       # set up local AI
prospector ai --key sk-or-...               # save a cloud key
prospector ai --route qualify=local         # pin one agent
prospector load companies.csv               # start from a list you have
prospector run                              # every agent
prospector run qualify --limit 5            # one agent, five companies
prospector run outreach                     # just redraft the messages
prospector run --dry-run                    # what's waiting, change nothing
prospector status                           # progress
prospector export -o leads.xlsx             # rebuild the spreadsheet
prospector doctor                           # check setup
prospector projects                         # each brief has its own database
```

---

## Where your files live

```
~/.prospector/
    active_project.txt
    shared/
        settings.env           your cloud key and AI setup, kept across projects
        engine/                the local AI engine and downloaded model
        engine.json            which engine was chosen, and why
    projects/<project>/
        prospector.db          the conveyor belt (WAL SQLite)
        .env                   keys, model choice, agent routing
        plan.json              the research plan
        exports/               the spreadsheets
        page_cache/            cached searches and pages
```

Uninstalling leaves this folder alone — your work is not thrown away with the
program.

---

## If something goes wrong

- **"Not possible on this PC"** for local AI — a real answer, not a failure. Use
  the cloud option.
- **Local AI download fails** — about 30MB for the engine plus 1–5GB for the
  model. It resumes; press Set up again. A direct model link can be pasted
  under Advanced if the automatic download cannot find one.
- **"Your cloud account is out of credit"** — add credit, pick a cheaper model,
  or move that agent to this PC on the AI screen.
- **Searches return nothing** — the run now stops and says so rather than
  finishing empty. Wait ten minutes and press Continue; nothing found is lost.
  If it keeps happening, a free [Brave Search API](https://brave.com/search/api/)
  key in `BRAVE_API_KEY` is used ahead of the free endpoint and does not get
  rate limited.
- **A company has no website** — not found confidently, deliberately not
  guessed, because a wrong site feeds false evidence to every stage after it.
  The **Researched from** column in All Leads tells you which rows this affects.
- **The app won't start after installing** — look at
  `%USERPROFILE%\.prospector\startup.log`.

Failures are counted per company per agent and retried up to three times across
runs, then left alone so one broken website cannot block the pipeline.

---

## Tests

```bash
python -m pytest tests -q
```

197 tests. Most of them are regressions: every one stands for something that
actually went wrong, and its docstring says what. Among them —

- a bare `"Match"` from the model used to become the *top* rating, because
  `"match"` is a substring of `"strong match"`
- `"Indian Oil Corporation"` and `"Oil India Limited"` collapsed into one row
- contacts were never checked against the pages they supposedly came from
- a blocked search endpoint filled the sheet with confident ratings built on
  nothing, and reported a clean run
- the plan form was overwritten every two seconds, silently discarding the one
  field the drafts depend on
- the time estimate promised five minutes for a run that took several hours

Before shipping, always verify in a **clean virtual environment** —
`build_installer.ps1` does this automatically. It is what catches a dependency
that only works because it happened to be installed already.
