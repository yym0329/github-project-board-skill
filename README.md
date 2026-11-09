# project-board: a GitHub Projects skill for Claude Code

This repository holds two things that work together:

- A [Claude Code](https://docs.claude.com/en/docs/claude-code) skill. It tells
  Claude how to keep a GitHub Projects board up to date: how to write cards,
  when to move them, and which changes need your approval.
- `board.py`, a small command-line tool that the skill uses. You can also run
  it yourself. It works with GitHub Projects (the newer "Projects", also
  called Projects v2), not the old classic project boards.

The tool calls the GitHub CLI (`gh`) for every action, so it uses your
existing `gh` login.

## Features

- List, show, create, move, edit, archive and delete cards. `list` shows
  cards in column order.
- Create a card as a draft or as a real issue, or add an existing issue or
  pull request.
- Set any editable field: single select, text, number, date and iteration.
  An empty value clears the field.
- Refer to a card by its node ID (`PVTI_...`), its issue or pull request
  number (`#12` or `12`), or part of its title. If a number matches no issue
  or pull request, it is tried as a title, so a card titled "2026" can still
  be found.
- Add, rename, remove and reorder the options of a single-select field (for
  example, the board's columns). Cards keep their values when you rename or
  reorder. Nothing changes until you pass `--apply`.
- Works with projects owned by a user or by an organization.
- Uses few API calls: it fetches the project's fields once per run and looks
  up a node ID with one small query. Archived cards are left out of lists
  and lookups, and a node ID from another project is refused.

## Requirements

- A recent [GitHub CLI](https://cli.github.com) with the `gh project`
  commands, logged in with the `project` scope. The tool was tested with
  gh 2.100.0. To add the scope: `gh auth refresh -h github.com -s project`
- `list --query` uses GitHub's Projects filter through the GraphQL API, not
  `gh project item-list --query` (which needs gh 2.87 or newer). It works on
  github.com; on GitHub Enterprise Server it needs a version whose API
  supports the filter (3.20 or newer, according to the gh help).
- Python 3.9 or newer. No extra packages are needed.

## Install

Copy `skills/project-board` into one of these places:

- `~/.claude/skills/` to use it in all your projects, or
- `.claude/skills/` inside one repository to use it only there.

For example:

```bash
git clone https://github.com/yym0329/github-project-board-skill.git
cp -r github-project-board-skill/skills/project-board ~/.claude/skills/
```

Claude Code loads the skill on its next start. You can run the tool on its
own without installing the skill:
`python3 skills/project-board/board.py --help`.

## Configure

The tool needs the board's owner (a user or organization login) and its
number. Both appear in the board's URL:
`github.com/users/OWNER/projects/NUMBER` or
`github.com/orgs/OWNER/projects/NUMBER`.

The simplest way is to write a config file in your repository:

```bash
python3 ~/.claude/skills/project-board/board.py init octocat 5 --repo octocat/hello-world
```

This creates `.project-board.json`:

```json
{"owner": "octocat", "number": 5, "repo": "octocat/hello-world"}
```

The tool looks for this file in the current directory and then in each
parent directory. The keys are:

| Key | Meaning |
|---|---|
| `owner` | User or organization that owns the project (required) |
| `number` | Project number (required) |
| `repo` | `OWNER/NAME` repository for `create --issue` (optional) |
| `status_field` | Single-select field that holds the columns (default `Status`) |

You can also set the board in other ways. The first source in this list
that sets anything supplies every key; keys are never mixed across sources:

1. Flags before the subcommand: `--owner`, `--number`, `--repo`,
   `--status-field`.
2. Environment variables: `PROJECT_BOARD_OWNER`, `PROJECT_BOARD_NUMBER`,
   `PROJECT_BOARD_REPO`, `PROJECT_BOARD_STATUS_FIELD`.
3. The nearest `.project-board.json`.
4. `~/.config/project-board/config.json`, for a board you use everywhere.

A source that sets any key must set both `owner` and `number`. For example,
`--owner other-org` alone is an error rather than a mix of `other-org` with
the number from your config file. Likewise `repo` and `status_field` come
only from the source that gave the owner and number.

## Usage

With the skill installed, ask Claude in plain words, for example "add a card
for the login bug", "what is in progress?" or "move the pagination card to
review". Claude runs `board.py` for you.

To run the tool yourself (`B` stands for the path to `board.py`):

```bash
B=~/.claude/skills/project-board/board.py
python3 $B fields                                 # columns, fields and options
python3 $B list --status "In progress"
python3 $B list --query "priority:P0 -status:Done"  # GitHub's filter syntax
python3 $B create "Search API: add pagination" --body "Goal: ..." --status Ready
python3 $B create "Fix login timeout" --issue     # real issue in the repo
python3 $B add https://github.com/octocat/hello-world/pull/7 --status "In review"
python3 $B move "#7" Done
python3 $B set "pagination" Priority P1
python3 $B set "pagination" "Target date" 2026-12-01
python3 $B set "pagination" Estimate ""           # clear a field
python3 $B archive "pagination"                   # prints the card's PVTI_ ID
python3 $B archive PVTI_lADOxxxx --undo           # restore by that ID
python3 $B options Status --rename "Todo=Backlog" --add "In review"   # dry run
python3 $B options Status --rename "Todo=Backlog" --add "In review" --apply
python3 $B --owner my-org --number 3 list         # another board, one time
```

If a title matches more than one card, the tool lists the matches and stops.
Use the node ID it prints instead.

GitHub's card listing can take about a minute to catch up after a change.
Right after you create, move, archive or restore a card, refer to it by the
node ID the tool printed rather than by title or number.

## Card-writing convention

The skill asks Claude to write cards in one short, fixed format, so anyone can
read a card without opening anything else:

- A specific title of about 60 characters or fewer, with no status words.
- A body of 60 to 120 words with labeled lines: `Goal`, `Done when`,
  `Result`, `Status`, `Links` and `Review`.
- Plain full sentences, the result first, numbers with their baseline and
  sample size, measured results kept apart from projections, and absolute
  dates with a timezone.
- A few readability rules: prefer clear over short, no human verbs for
  things, no stacks of three nouns, common phrases, keep "that" in relative
  clauses, and keep subjects near their verbs.

It also describes how work moves across the board: one card per reviewable
piece of work, left to right through the columns, few cards in progress, a
review by someone other than the author before Done, and old Done cards
archived. See `skills/project-board/SKILL.md` for the full text.

## Limitations

- **Views can't be changed.** GitHub has no API for board or table layout,
  grouping or which fields cards show. Change these in the browser.
- **Archived cards can't be listed.** GitHub's item listing drops archived
  cards (sometimes after a short delay), so they can't be found by title or
  number. Keep the `PVTI_` ID that `archive` prints if you may want to undo
  it; `archive <ID> --undo` restores the card.
- **Listing lag.** `list` and title or number lookups can trail a change by
  about a minute. Use the printed node ID right after a change.
- **Rate limits.** GitHub's GraphQL API allows 5,000 points per hour. A
  `list`, or a lookup by title or number, reads the whole board and costs
  about 2 points per 100 cards (measured with a dry run). A lookup by node ID
  is one small query. For many edits in a row, use the node IDs that `list`
  prints.
- **Large boards.** `list` and title lookups read at most 1,000 cards.
- Fields such as assignees, labels and milestones belong to the issue, not the
  project. Change them with `gh issue edit`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests replace `gh` with a fake, so they need no network or login.

## License

MIT. See [LICENSE](LICENSE).
