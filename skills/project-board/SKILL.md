---
name: project-board
description: Manage a GitHub Projects (v2) board or kanban through the gh CLI — list or show cards, create draft or issue cards, add issues and pull requests, move cards between columns, set fields, archive or delete cards, and change the board's columns and fields. Use whenever the user mentions the project board, kanban, a card, item or task on it, or asks to create, move, pick up, review or close out work there.
---

# Project board

Drive the board with the helper script next to this file. It resolves every
project, field and option ID for you. It needs Python 3.9+ (stdlib only) and
`gh` with the `project` token scope.

```bash
B="<skill-dir>/board.py"                # the directory holding this SKILL.md
python3 $B fields                       # fields, types and options
python3 $B list [--status "In progress"] [--query "priority:P0 -status:Done"]
python3 $B list --archived              # archived cards only
python3 $B show <item>                  # all field values, URL and body
python3 $B create "Title" [--body "..."] [--status Backlog] [--issue]
python3 $B add <issue-or-PR-URL> [--status "In review"]
python3 $B move <item> Done
python3 $B set <item> <Field> <value>   # single select, text, number, date, iteration; '' clears
python3 $B edit <item> [--title ...] [--body ...]   # drafts only
python3 $B archive <item>
python3 $B archive <item> --undo        # matches archived cards only
python3 $B delete <item>
python3 $B options <Field> [--add X] [--rename Old=New] [--remove X] [--order A,B,C] [--apply]
```

`<item>` is a `PVTI_...` node ID, an issue or PR number (`#12` or `12`), or
a title substring that matches exactly one card (an exact title match wins;
a number that matches no issue or PR is tried as a title). Archived cards are
skipped, except by `list --archived` and `archive --undo`. On several matches
the helper lists them and stops; then use the node ID.

## Configuration

The helper needs to know which board to use. If it reports that no board is
configured, ask the user for the owner (user or organization login) and the
project number (from `github.com/users/<owner>/projects/<number>` or
`github.com/orgs/<owner>/projects/<number>`), then run:

```bash
python3 $B init <owner> <number> [--repo owner/name] [--status-field Status]
```

This writes `.project-board.json` in the current directory. The helper finds
it by searching upward. Global flags (`--owner`, `--number`, `--repo`,
`--status-field`, placed before the subcommand) and `PROJECT_BOARD_*`
environment variables override the file; `~/.config/project-board/config.json`
is the fallback. The first source that sets anything supplies every key, and
it must set both owner and number. `repo` is needed only for
`create --issue`.

## Learn the board first

Run `fields` before the first change in a session. Use the column and option
names it prints; don't assume them. This skill uses a typical column set as an
example: Backlog, Ready, In progress, In review, Done. If the board uses other
names, map them by meaning (for example "Todo" is Backlog or Ready, "Doing" is
In progress, "Shipped" is Done) and use the board's real names in commands.
If the columns live in a field other than `Status`, set `status_field`.

## Writing a card

A reader who opens only the card should understand what the task is, why it
matters, where it stands and where to look next. Keep it short: the
repository, documents and linked issues hold the detail.

**Title** (at most about 60 characters). Name the outcome specifically:
"Search API: add pagination to /items", not "API stuff" or "Fix things". Put
the subject first and the scope after a colon. Don't put status words ("WIP",
"done") in the title; the column shows status.

**Body.** Use these labeled lines, in this order, and leave out any that
don't apply. Aim for 60–120 words and stay under about 150.

```
Goal: <one or two sentences: what this produces and why it matters>
Done when: <a check someone else can verify: a merged PR, a passing test, a published report, a decision>
Result: <for finished work: the headline numbers with their comparison, then one sentence of conclusion>
Status: <for unfinished work: progress with a dated timestamp, and what it waits on>
Links: <branch or PR, issue, document, data location; full paths or URLs>
Review: <added at review time: date, verdict, and any caveat>
```

Wording rules:

- Write plain, full sentences and put the result first.
- Spell out each abbreviation the first time it appears in a card, unless
  everyone on the project already uses it. Say what a metric means.
- Give a number together with what it is compared to and the sample size:
  "p95 latency 180 ms vs 240 ms before (−25%, 10k requests)". A number
  without a baseline tells the reader nothing.
- Separate measured results from expectations ("projected", "estimated").
- Write absolute dates with a timezone ("2026-03-04 14:00 UTC"), not
  "yesterday". Use paths, URLs and commit hashes, not "the earlier run".
- Add a caveat only when it changes how someone reads the result, and put it
  on its own line.

Readability rules (from Clark, Martin & Mahowald 2026, "Why is AI-generated
language so hard to understand?", arXiv:2610.29162):

- When a shorter wording and a clearer one compete, pick the clearer one.
  Cut content, not grammar.
- Don't give things human or animal verbs ("the cache swallows the error").
  Name what actually happens.
- Don't stack three or more nouns ("token refresh failure bug"). Unpack them
  with "of", "for" or a verb.
- Use common phrases. Don't coin new ones or turn nouns into verbs ("ticket
  this"). Define any new term once.
- Keep "that" in relative clauses ("the fix that the reviewer suggested").
- Keep each subject close to its verb. Split a sentence rather than wedge a
  long clause into the middle.

## Working practice

- **One card is one deliverable** that someone could review: a merged change,
  a finished analysis, a document or a decision. Split work that will take
  more than about a week, or that has several separate outputs. Fold trivial
  steps into the card they belong to.
- **Columns mean:**
  - Backlog: wanted, but not scheduled.
  - Ready: scheduled next. Every input is in place, so it can start now.
  - In progress: someone (or a running job) is working on it now.
  - In review: the work is finished and waits for review.
  - Done: reviewed and accepted.
- **Pull work through the board, left to right.** Take the highest-priority
  card from Ready. Don't skip In review.
- **Keep In progress small:** about three cards per person. If more pile up,
  finish some or move them back before starting new work.
- **Keep the board current.** Update a card when its state changes (start,
  finish, block, cancel), not in a batch at the end of the day. Add a
  `Status:` line with a dated timestamp when progress matters.
- **Blocked work stays in its column.** Start its `Status:` line with
  "Blocked:", name the blocker, and say who must act.
- **Cancelled or superseded work:** add one line saying why, then archive the
  card. Don't delete it.
- **Priorities and dates** (when the board has such fields): set a priority on
  every card in Ready and In progress. Set a start date when a card enters In
  progress. Set a target date only for a real deadline or projection.
- **Review before Done, by someone other than the author.** If you can start
  subagents, start a fresh one for the review. Give it the artifacts (code,
  PR, data, report) and the card's claims, not your own conclusion. Ask it to
  check each claim against the artifacts and to give a counterexample for any
  finding. If the review passes, add the `Review:` line and move the card to
  Done. If it fails, move the card back to In progress with the finding.
  Skip the review only when the user says so for that card.
- **Archive** Done cards about two weeks after they are done, so the board
  shows current work.

## Board operations

- **Read before writing.** Run `list` before creating a card so you don't
  duplicate one.
- **Draft or issue.** Create a draft card by default. Use `--issue` (a real
  issue in the configured repository) only when the work will produce a
  branch or PR, or the user asks. Add existing issues and PRs with `add`;
  don't re-create them.
- **New cards** go to the first column (Backlog) unless the user names one.
- **Picking up a task:** `show` it, move it to In progress, then do the work.
  When a PR is open, move it to In review. Tell the user each move.
- **Destructive actions need the user's OK first:** `delete`,
  `options --remove`, and `gh project field-delete`. Prefer `archive`, which
  `archive --undo` reverses.
- **Report each change** as "title: old column → new column".
- **API limits.** GitHub's GraphQL API allows 5,000 points per hour. A
  `list`, or a lookup by title or number, reads the whole board (about 2
  points per 100 cards). In bulk edits use the `PVTI_` node IDs that `list`
  prints; each node-ID lookup is one small query.

## Managing the board itself

- **Columns and other single-select options:** use `options`. It is a dry run
  until `--apply`. It sends existing options back with their IDs, so cards
  keep their values through renames and reorders. A removed option clears
  that value on its cards. Renames run first, so `--remove` and `--order`
  accept new names; renaming onto an existing name is refused.
- **Fields** (`gh project` commands; use the board's owner and number):
  ```bash
  gh project field-create <number> --owner <owner> --name Area \
    --data-type SINGLE_SELECT --single-select-options "Frontend,Backend"
  gh project field-create <number> --owner <owner> --name Notes --data-type TEXT
  gh project field-delete --id <PVTF_/PVTSSF_ id>   # destructive: confirm first
  gh project edit <number> --owner <owner> --title ... --description ... --readme ...
  ```
- **Views** (board or table layout, grouping, what cards show) have no API.
  Ask the user to change them in the browser.

## Auth

`gh` needs the `project` token scope. If `gh` reports it missing, run
`gh auth refresh -h github.com -s project` in the background, read the
one-time code from its output, and give the user the code and
`https://github.com/login/device`. They finish it in their own browser.
