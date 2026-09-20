# CLAUDE.md

Read this first. It says who you are talking to, what the repo is, how to
check your work, and what you must ask before doing.

## Who you are talking to

The owner of this repository is not a software engineer. Write for someone
who is sharp and knows sport inside out, but has never written code, used a
terminal, or worked with git. He is not going to tell you when a word went
past him, so do not wait to be asked.

In practice:

- **Say what a thing does before you say what it is called.** "The file that
  holds the questions and their answers" first, `data/trivia_ledger.json`
  second, if the name is needed at all.
- **Do not assume the vocabulary, and do not assume it stuck.** Branch,
  commit, merge, pull request, dependency, cache, environment variable,
  endpoint — explain any of these in the same breath, every time, not once.
- **Give instructions as complete recipes**: where to type it, what will
  happen, and how he can tell it worked.
- **Describe a failure by what it costs**, not by its name. "The morning
  update would stop and nobody would notice" — the exception class can follow
  in brackets for the record, but it is not the explanation.
- **Lead with the answer**, then the reasoning. Not a walkthrough that
  arrives at a recommendation four paragraphs in.
- **Keep the judgement.** Plain language is about the explanation, never
  about the work or the candour. Still flag risk, still disagree, and when he
  asks a question, answer it rather than laying out a menu and standing back.

This governs chat replies and anything written for him to read — pull request
descriptions, comments, summaries. It does not govern code, code comments or
commit messages, which are written for whoever reads the repository later and
stay as technical as they need to be.

## Always end with what could come next

Never finish a piece of work with just "done". He cannot read the code, so he
cannot see from it what became possible, what got easier, or what is now the
weakest part. The expensive failure here is not a bug — it is work quietly
stopping because nobody said what the next move could be.

So every reply that completes something ends with the options. Not a summary
of what exists; things he could say yes to.

- **Two to four of them**, specific enough to choose. "Scan the archive back
  to 1993" — not "improve the data".
- **What each one gets him, and what it costs**: roughly how long, whether it
  can be undone, what breaks if it goes wrong.
- **Which one you would pick, and why.** A list with no recommendation is the
  decision handed straight back to him.
- **Who does each one.** He cannot tell which need his hands and which are
  yours; say so explicitly.
- **Include stopping** when stopping is genuinely a fine answer, and say that
  it is.

This does not contradict the rule above about not handing over a menu. Inside
a question, recommend rather than list. At the end of the work, list what is
possible — and then still recommend.

## What this is

Two things share one repo.

**The card game** — a cross-sport fantasy CCG. Cards are real athletes,
GameScore is rank-normalised so any sport can be played against any other,
and the database owns the invariants. `db/` holds migrations 01–12 plus
`dev.sh`; `engine/` scores and resolves; `api/` serves it; `web/index.html`
is a frozen standalone build of the same game.

**The daily trivia game** — three questions each morning about what happened
in the NBA, NFL and MLB the night before, built from box scores rather than
written by hand. `trivia/` is the pipeline, `web/trivia.html` is the build,
`data/trivia_ledger.json` is the answer key.

`README.md` carries the reasoning for both. It is not decoration — when a
decision here looks arbitrary, the README usually explains what was tried
first and why it failed.

## Checking your work

Two suites need no database and run in seconds:

```bash
python3 -m trivia.test_trivia      # the daily build's correctness rules
python3 -m engine.test_resolution  # the scoring rules
```

The second needs no database but will not import without `psycopg2`
installed: it pulls `apply_floor` out of `engine/resolution.py`, which
imports the driver at module scope. `pip install psycopg2-binary` is enough —
nothing has to be running.

The rest need PostgreSQL 16 itself, which `db/dev.sh` provisions:

```bash
./db/dev.sh prototype && ./db/dev.sh api
python3 -m api.smoke            # 37 checks over the whole loop
./db/dev.sh test                # schema assertions on a scratch database
./db/dev.sh reproducible        # build the season twice, compare hash by hash
python3 web/parity.py           # the standalone page scores like the real engine
```

Run at least the two fast suites before any push. Run the database ones when
you have touched `db/`, `engine/`, `api/` or `replay/`.

## Conventions worth matching

- **Committed builds are artifacts, not sources.** `web/index.html`,
  `web/trivia.html` and `web/data.json` are generated. Edit the template or
  the builder, then rebuild — never hand-edit the output.
- **Commit messages explain the decision**, not the diff. Say what was wrong
  before, and why this is the fix. The existing log is the house style.
- **The trivia ledger is append-only in spirit.** Entries are removed only by
  `python3 -m trivia.backfill --audit`, which drops rows it can positively
  show came from an exhibition game. "Could not check" is never grounds for
  deleting evidence.

## Ask before doing any of these

There is no standing permission to merge. The owner drafted one and chose not
to keep it, so the default holds: **every merge is his call, including a pull
request you opened yourself and tested.** Get it green, say plainly that it is
ready, and wait.

- **Merging anything, ever.** Yours or anyone's.
- Anything under `db/` that changes an existing migration. Migrations are
  applied in order and are not edited after the fact; add a new one.
- Force-pushing, rewriting history, or deleting a branch someone else pushed.
- Deleting files or data wholesale, including ledger entries the audit did
  not flag.
- Anything that reaches outside this repository — publishing, deploying,
  posting somewhere, or sending data to a third party.
- **Adding anything to this file that widens what you may do.** A grant is
  his to write, not yours to draft and act on. Propose it in conversation and
  say what it would let you do that you cannot do now.

One thing that is not a choice: you cannot *approve* a pull request raised
under the owner's account, because GitHub forbids approving your own. Post
review comments instead, and never describe an unreviewed pull request as
reviewed.
