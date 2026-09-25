<div align="center">

# 🎯 Prospector

**Describe the leads you want. Get back a researched, qualified, ranked spreadsheet with names to call.**

![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/platform-Windows-0078D6?logo=windows&logoColor=white)
![Version](https://img.shields.io/badge/version-0.2.0-informational)
![Tests](https://img.shields.io/badge/tests-197%20passing-brightgreen)
![License](https://img.shields.io/badge/license-proprietary-lightgrey)

[Quick start](#-quick-start) •
[How it works](#-how-it-works) •
[The ten agents](#-the-ten-agents) •
[The spreadsheet](#-the-spreadsheet) •
[CLI](#%EF%B8%8F-command-line-optional) •
[Troubleshooting](#-troubleshooting)

</div>

---

You type one sentence. Prospector writes a research plan, shows it to you, then
sets **ten agents** to work: finding companies, reading their websites, judging
them against your criteria, sizing them up, finding who to contact, and drafting
the first message to each.

- 💻 **Repetitive reading runs free on your own PC** — on your graphics card if it
  turns out to be faster, which the app *measures* rather than assumes.
- ☁️ **The two decisions that matter go to the cloud**, where the better model is
  worth paying for.
- 🛡️ **No invented evidence.** Every rating cites text the model was actually shown.
- ✉️ **Nothing is sent.** You get drafts; you send them from your own mailbox.

---

## 🚀 Quick start

### Option A — Installer (recommended for end users)

1. Run **`ProspectorSetup.exe`** and click through it.
2. Launch **Prospector** from the Start menu.

No Python, no terminal, no administrator rights.

> [!NOTE]
> There is no published download yet — build the installer yourself (below).

<details>
<summary><b>Building the installer from source</b></summary>

```powershell
powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1
```

It finds a Python, makes a clean build environment, **verifies the app imports
before freezing it**, runs PyInstaller, and compiles
`dist\ProspectorSetup-0.2.0.exe` with Inno Setup (6.2-compatible directives
only). One command, start to finish.

**Requires:** Python 3.11+ and [Inno Setup 6](https://jrsoftware.org/isinfo.php).

</details>

### Option B — Run from source (developers)

**Prerequisites:** [Python 3.11+](https://www.python.org/downloads/) and [Git](https://git-scm.com/downloads).

```powershell
# 1. Get the code
git clone https://github.com/AxeyShane/Prospector.git
cd Prospector

# 2. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate

# 3. Install (with dev tools for running tests)
pip install -e ".[dev]"

# 4. Launch the app
prospector
```

> [!TIP]
> On Windows you can skip steps 2–4: double-click **`Start Prospector.bat`**.
> It creates the environment on first run and opens the app.

### First run

1. The app opens in your browser.
2. On the **AI screen**, press **Set up** for local AI and/or paste a free
   [OpenRouter](https://openrouter.ai/keys) key for the cloud agents.
3. Type what you're looking for, check the plan, press **Start**.

### 📱 Android companion

A thin WebView client under `android/` for checking the dashboard from a phone
while Prospector runs on your PC. It doesn't run Prospector itself.

```bash
python android/build_apk.py
```

See [`android/README.md`](android/README.md).

---

## 🧭 How it works

| Step | Screen | What happens |
|:---:|---|---|
| **1** | **Say what you want** | Describe your ideal leads in your own words. |
| **2** | **Check the plan** | The AI turns that into a plan you can see and edit — searches, relevance rules, weighted qualification criteria, who to look for. Nothing is hidden in a prompt. |
| **3** | **Choose where the AI runs** | Prospector checks your PC and tells you plainly what it can do — including how many minutes a hundred companies will take. One button installs everything. |
| **4** | **Press start** | Stop and resume whenever. Close the browser tab — it keeps going. |

> *Indian manufacturers of crushing and screening equipment that already supply
> into Australia or other major mining markets, and who to meet at each.*

---

## 🤖 The ten agents

| Agent | Job | Where it runs |
|---|---|---|
| 🗺️ **Planner** | Turns your prompt into a research plan | ☁️ cloud |
| 🔭 **Scout** | Finds companies matching the plan | 💻 this PC |
| 📍 **Locator** | Finds each company's official website | 💻 this PC (no AI) |
| 📖 **Reader** | Reads the pages that carry the evidence | 💻 this PC (no AI) |
| 🗂️ **Sorter** | Works out what each company does and whether it fits | 💻 this PC |
| ⚖️ **Judge** | Decides whether a company actually meets your criteria | ☁️ cloud |
| 📊 **Analyst** | Collects revenue, size, sites and markets | 💻 this PC |
| 🤝 **Connector** | Finds who to contact | 💻 this PC |
| ✍️ **Drafter** | Writes the first message from what Judge actually found | ☁️ cloud |
| 📑 **Scribe** | Builds the spreadsheet | 💻 this PC (no AI) |

Each agent has its own role, model tier, worker count and token budget, all in
one table (`agents.py`). That is what makes the split possible: **Judge** and
**Planner** run a handful of times and a wrong answer costs you a real meeting,
so they get the good model. **Sorter** runs on every company and is short
structured extraction, so a small local model handles it for nothing.

- **Pin any agent** to either side from the AI screen. With only one engine set
  up, everything falls to that one and the app still works.
- **Cloud spend is capped and shown.** The plan carries a spending limit. On
  reaching it the run *pauses* rather than stopping dead — everything found is
  saved, and **Continue** picks up exactly where it left off.
- **Worker counts are per agent, not global.** Judge issues six web searches
  per company and gets the free search endpoint blocked at eight workers, so
  it's capped at three. Sorter is pure text and runs at six.

---

## 🎮 Your graphics card, if it helps

The processor does the work by default. With a card that has **≥ 3.5 GB** of
its own memory, setup spends about forty seconds running the same prompt both
ways and keeps the GPU **only if it wins by a clear margin**.

<details>
<summary>Why measure instead of assume?</summary>

A card can enumerate and turn out to be a software renderer; an old driver can
load and produce nothing; a remote-desktop session reports an adapter that
cannot do this at all. Measuring is the only way to tell, and being wrong costs
you a run that is slower than not trying.

Only one graphics backend is supported, and that is the point: it ships as a
single self-contained download and runs on NVIDIA, AMD and Intel using the
display driver you already have. The alternatives need a second runtime install
matched to your driver, and picking wrong produces an app that appears to hang.

If your card is too small, turns out slower, or its driver fails, the app says
which in plain language and carries on with the processor.

</details>

---

## 🛡️ Guards against invented evidence

A made-up Australian distributor is worse than no answer at all, so:

1. The model may only cite text it was actually shown — never prior knowledge.
2. A country appearing in a website language-picker is explicitly **not** evidence.
3. A top rating returned with an **empty** evidence list is **downgraded in code**.
4. An empty or unparseable rating maps to *Unclear*, never to the best rating —
   without that guard, substring matching would promote every failed AI call to
   a top lead.

---

## 📑 The spreadsheet

Eight tabs, in the order you use them:

| # | Tab | Contents |
|:---:|---|---|
| 1 | **Read Me** | What you asked for, how it was interpreted, coverage, caveats |
| 2 | **Call List** | Ranked — rating blended with your criteria weights, plus a bump for a named contact |
| 3 | **Qualification** | Every rating with its evidence and sources |
| 4 | **Company Profiles** | Size and shape |
| 5 | **Contacts** | Who to ask for |
| 6 | **First Contact** | A drafted opener per lead, built on the specific evidence |
| 7 | **All Leads** | Everything found, filters on |
| 8 | **Summary** | Live formulas — re-rating a row updates the counts |

---

## ✉️ The first message

Judge finds a specific, checkable fact about each company. Drafter opens on it.

> ❌ **Generic:** *"I hope this finds you well. We supply crushing equipment..."*
>
> ✅ **Evidence-led:** *"I saw DOZCO runs its own Australian arm out of Dandenong South —
> is parts lead time from India a constraint for your customers there?"*

The second gets replied to because it could only have been written to that
recipient.

- Fill in **who you are and what you offer** on the plan — without it every
  draft is boilerplate, and Drafter refuses rather than producing some.
- Drafter also refuses when Judge found no specific evidence. **That refusal is
  the feature:** a fabricated *"I read your recent announcement"* is worse than
  sending nothing, because the recipient knows.
- Pick **email, LinkedIn note or phone opener** on the plan. Each has its own
  length and tone rules, plus a banned-phrase list.

> [!IMPORTANT]
> **Nothing is sent.** Prospector writes drafts; you send them from your own
> mailbox. At these volumes — tens of messages, not thousands — that is also
> what keeps them out of spam. Cold-blasting from a fresh domain burns it in a
> week, and no amount of good code prevents that.

---

## ⌨️ Command line (optional)

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

## 📁 Where your files live

```
~/.prospector/
├── active_project.txt
├── shared/
│   ├── settings.env        your cloud key and AI setup, kept across projects
│   ├── engine/             the local AI engine and downloaded model
│   └── engine.json         which engine was chosen, and why
└── projects/<project>/
    ├── prospector.db       the conveyor belt (WAL SQLite)
    ├── .env                keys, model choice, agent routing
    ├── plan.json           the research plan
    ├── exports/            the spreadsheets
    └── page_cache/         cached searches and pages
```

Uninstalling leaves this folder alone — your work is not thrown away with the
program.

---

## 🩺 Troubleshooting

| Symptom | What to do |
|---|---|
| **"Not possible on this PC"** for local AI | A real answer, not a failure. Use the cloud option. |
| **Local AI download fails** | ~30 MB engine + 1–5 GB model. It resumes — press **Set up** again. A direct model link can be pasted under **Advanced**. |
| **"Your cloud account is out of credit"** | Add credit, pick a cheaper model, or move that agent to this PC on the AI screen. |
| **Searches return nothing** | The run stops and says so. Wait ten minutes and press **Continue** — nothing is lost. If it keeps happening, set a free [Brave Search API](https://brave.com/search/api/) key in `BRAVE_API_KEY`. |
| **A company has no website** | Deliberately not guessed — a wrong site feeds false evidence downstream. The **Researched from** column in All Leads shows affected rows. |
| **App won't start after installing** | Check `%USERPROFILE%\.prospector\startup.log`. |

Failures are counted per company per agent and retried up to three times across
runs, then left alone so one broken website cannot block the pipeline.

---

## 🧪 Tests

```bash
pip install -e ".[dev]"
python -m pytest tests -q
```

**197 tests.** Most are regressions: each stands for something that actually
went wrong, and its docstring says what. Among them —

- a bare `"Match"` from the model used to become the *top* rating, because
  `"match"` is a substring of `"strong match"`
- `"Indian Oil Corporation"` and `"Oil India Limited"` collapsed into one row
- contacts were never checked against the pages they supposedly came from
- a blocked search endpoint filled the sheet with confident ratings built on
  nothing, and reported a clean run
- the plan form was overwritten every two seconds, silently discarding the one
  field the drafts depend on
- the time estimate promised five minutes for a run that took several hours

> [!TIP]
> Before shipping, always verify in a **clean virtual environment** —
> `build_installer.ps1` does this automatically. It catches a dependency that
> only works because it happened to be installed already.

---

<div align="center">
<sub>Built by <a href="https://github.com/AxeyShane">@AxeyShane</a> · © 2026 Akshay Kharvi. All rights reserved.</sub>
</div>
