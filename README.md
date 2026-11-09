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

- List, show, create, move, edit, archive and delete cards.
- Create a card as a draft or as a real issue, or add an existing issue or
  pull request.
- Set any editable field: single select, text, number, date and iteration.
  An empty value clears the field.
- Refer to a card by its node ID (`PVTI_...`), its issue number (`#12`), or
  part of its title.
- Add, rename, remove and reorder the options of a single-select field (for
  example, the board's columns). Cards keep their values when you rename or
  reorder. Nothing changes until you pass `--apply`.
- Works with projects owned by a user or by an organization.
- Uses few API calls: it fetches the project's fields once per run and looks
  up a node ID with one small query.

## Requirements

- [GitHub CLI](https://cli.github.com) 2.40 or newer, logged in with the
  `project` scope. To add the scope:
  `gh auth refresh -h github.com -s project`
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

You can also set the board in other ways. When several are set, the first
one in this list wins, key by key:

1. Flags before the subcommand: `--owner`, `--number`, `--repo`,
   `--status-field`.
2. Environment variables: `PROJECT_BOARD_OWNER`, `PROJECT_BOARD_NUMBER`,
   `PROJECT_BOARD_REPO`, `PROJECT_BOARD_STATUS_FIELD`.
3. The nearest `.project-board.json`.
4. `~/.config/project-board/config.json`, for a board you use everywhere.

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
python3 $B archive PVTI_lADOxxxx --undo
python3 $B options Status --rename "Todo=Backlog" --add "In review"   # dry run
python3 $B options Status --rename "Todo=Backlog" --add "In review" --apply
python3 $B --owner my-org --number 3 list         # another board, one time
```

If a title matches more than one card, the tool lists the matches and stops.
Use the node ID it prints instead.

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
- **Archived cards can't be listed.** The API does not return them, so keep
  the `PVTI_` ID that `archive` prints if you may want to undo it.
- **Rate limits.** GitHub limits GraphQL API use per hour. Looking up a card
  by title or number lists the whole board, so for many edits in a row, use
  node IDs.
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
