#!/usr/bin/env python3
"""Manage a GitHub Projects (v2) board from the command line through `gh`.

Stdlib only. Every subcommand shells out to the GitHub CLI, which needs the
`project` token scope (`gh auth refresh -h github.com -s project`).

The board (owner, number, and optionally repo and status_field) comes from
exactly one of these sources, highest priority first: the global flags
--owner/--number/--repo/--status-field; the environment variables
PROJECT_BOARD_OWNER/_NUMBER/_REPO/_STATUS_FIELD; the nearest
.project-board.json found by searching upward from the current directory; and
~/.config/project-board/config.json. A source that sets any key must set both
owner and number; keys are never mixed across sources. `board.py init OWNER
NUMBER` writes .project-board.json in the current directory.

Items can be named by node ID (PVTI_...), by issue or PR number (#12 or 12),
or by a case-insensitive title substring that matches exactly one item. An
exact title match wins over substring matches, and a number that matches no
issue or PR is tried as a title. Archived items are skipped; restore one with
`archive PVTI_... --undo`, using the node ID that `archive` printed. GitHub's
item listing can lag a change by about a minute, so right after a change
refer to the item by its printed node ID.
"""

from __future__ import annotations

import argparse
import dataclasses
import functools
import json
import os
import pathlib
import subprocess
import sys
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

CONFIG_FILENAME = ".project-board.json"
USER_CONFIG = pathlib.Path(".config", "project-board", "config.json")
ENV_PREFIX = "PROJECT_BOARD_"
CONFIG_KEYS = ("owner", "number", "repo", "status_field")
DEFAULT_STATUS_FIELD = "Status"
ITEM_LIMIT = 1000
PAGE_SIZE = 100

MISSING_CONFIG_HELP = """\
No project board configured (owner and number are required).
Set the board in one of these ways, highest priority first:
  flags:        board.py --owner LOGIN --number N [--repo OWNER/NAME] ...
  environment:  PROJECT_BOARD_OWNER, PROJECT_BOARD_NUMBER,
                PROJECT_BOARD_REPO, PROJECT_BOARD_STATUS_FIELD
  project file: .project-board.json in this directory or a parent
                (create it with: board.py init LOGIN N [--repo OWNER/NAME])
  user file:    ~/.config/project-board/config.json
Both files hold JSON such as {"owner": "octocat", "number": 1}.
All keys come from the first source that sets any of them."""


class BoardError(Exception):
    """A user-facing error; main() prints it and exits with status 1."""


@dataclasses.dataclass(frozen=True)
class Config:
    """Which board to manage.

    Attributes:
        owner: Login of the user or organization that owns the project.
        number: Project number, as in .../projects/<number>.
        repo: OWNER/NAME repository for `create --issue`, or None.
        status_field: Name of the single-select field that holds the columns.
        source: Where the configuration came from, for messages.
    """

    owner: str
    number: int
    repo: Optional[str] = None
    status_field: str = DEFAULT_STATUS_FIELD
    source: str = ""


def find_project_config(start: pathlib.Path) -> Optional[pathlib.Path]:
    """Returns the nearest .project-board.json at or above `start`, if any."""
    for directory in [start, *start.parents]:
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _read_config_file(path: pathlib.Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise BoardError(f"cannot read {path}: {e}") from e
    if not isinstance(data, dict):
        raise BoardError(f"{path} must hold a JSON object")
    unknown = set(data) - set(CONFIG_KEYS)
    if unknown:
        raise BoardError(f"{path}: unknown keys {sorted(unknown)}; "
                         f"allowed: {', '.join(CONFIG_KEYS)}")
    return data


def load_config(flags: Mapping[str, Any],
                env: Mapping[str, str],
                cwd: pathlib.Path,
                home: pathlib.Path) -> Config:
    """Resolves the board configuration from one source.

    Sources are tried in order: flags, env, the nearest project file, the
    user file. The first source that sets any key supplies every key, so a
    flag or environment variable never combines with a file's values. A file
    is read only when every higher source is empty, so a broken file does
    not block flags or env.

    Args:
        flags: Values from the global CLI flags; None means not given.
        env: Environment variables (normally os.environ).
        cwd: Directory where the upward search for .project-board.json starts.
        home: Home directory holding .config/project-board/config.json.

    Returns:
        The configuration from the first source that sets anything.

    Raises:
        BoardError: If no source sets the board, the chosen source lacks
            owner or number, the number is not an integer, or a config file
            is unreadable.
    """
    def sources() -> Iterator[Tuple[str, Mapping[str, Any],
                                    Mapping[str, str]]]:
        # A generator, so files are read only if no earlier source is used.
        yield ("flags", flags,
               {k: "--" + k.replace("_", "-") for k in CONFIG_KEYS})
        yield ("environment",
               {k: env.get(ENV_PREFIX + k.upper()) for k in CONFIG_KEYS},
               {k: ENV_PREFIX + k.upper() for k in CONFIG_KEYS})
        for path in (find_project_config(cwd), home / USER_CONFIG):
            if path and path.is_file():
                yield (str(path), _read_config_file(path),
                       {k: k for k in CONFIG_KEYS})

    for source, layer, names in sources():
        values = {k: layer.get(k) for k in CONFIG_KEYS
                  if layer.get(k) not in (None, "")}
        if not values:
            continue
        missing = [names[k] for k in ("owner", "number") if k not in values]
        if missing:
            given = ", ".join(names[k] for k in values)
            raise BoardError(
                f"{source} sets {given} but not {' or '.join(missing)}. "
                "The board's owner and number must come from the same "
                "source; other sources are not combined with it.")
        try:
            number = int(values["number"])
        except (TypeError, ValueError):
            raise BoardError(f"{source}: project number must be an integer, "
                             f"got {values['number']!r}") from None
        return Config(owner=str(values["owner"]), number=number,
                      repo=values.get("repo"),
                      status_field=values.get("status_field",
                                              DEFAULT_STATUS_FIELD),
                      source=source)
    raise BoardError(MISSING_CONFIG_HELP)


def gh(*args: str, stdin: Optional[str] = None) -> str:
    """Runs `gh` with `args` and returns its stdout.

    Raises:
        BoardError: If gh is missing or exits with a nonzero status.
    """
    try:
        proc = subprocess.run(["gh", *args], input=stdin,
                              capture_output=True, text=True)
    except FileNotFoundError:
        raise BoardError("the GitHub CLI `gh` is not installed or not on "
                         "PATH; see https://cli.github.com") from None
    if proc.returncode != 0:
        raise BoardError(proc.stderr.strip()
                         or f"gh {' '.join(args)} failed")
    return proc.stdout


# The project may belong to a user or an organization, so queries go through
# repositoryOwner and ask both concrete types.
OWNER_WRAPPER = """
  repositoryOwner(login: $owner) {
    ... on User { projectV2(number: $number) { ...board } }
    ... on Organization { projectV2(number: $number) { ...board } }
  }"""

PROJECT_QUERY = """
query($owner: String!, $number: Int!) {%s
}
fragment board on ProjectV2 {
  id
  fields(first: 100) { nodes {
    ... on ProjectV2FieldCommon { id name dataType }
    ... on ProjectV2SingleSelectField { options { id name color description } }
    ... on ProjectV2IterationField {
      configuration { iterations { id title startDate } } }
  } }
}""" % OWNER_WRAPPER

FIELD_NAME = "field { ... on ProjectV2FieldCommon { name } }"

ITEM_FRAGMENT = """
fragment item on ProjectV2Item {
  id
  isArchived
  project { id }
  fieldValues(first: 50) { nodes {
    ... on ProjectV2ItemFieldSingleSelectValue { name %(f)s }
    ... on ProjectV2ItemFieldTextValue { text %(f)s }
    ... on ProjectV2ItemFieldNumberValue { number %(f)s }
    ... on ProjectV2ItemFieldDateValue { date %(f)s }
    ... on ProjectV2ItemFieldIterationValue { title %(f)s }
  } }
  content {
    __typename
    ... on DraftIssue { id title body }
    ... on Issue { id title body number url
      assignees(first: 10) { nodes { login } } }
    ... on PullRequest { id title body number url
      assignees(first: 10) { nodes { login } } }
  }
}""" % {"f": FIELD_NAME}

ITEM_QUERY = """
query($id: ID!) { node(id: $id) { ...item } }""" + ITEM_FRAGMENT

# %(qvar)s and %(qarg)s add the Projects filter only when one is given.
ITEMS_QUERY = """
query($owner: String!, $number: Int!, $cursor: String%(qvar)s) {%(wrapper)s
}
fragment board on ProjectV2 {
  items(first: %(page)d, after: $cursor%(qarg)s) {
    pageInfo { hasNextPage endCursor }
    nodes { ...item }
  }
}""" + ITEM_FRAGMENT

OPTIONS_MUTATION = """
mutation($field: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]) {
  updateProjectV2Field(input: {fieldId: $field,
                               singleSelectOptions: $options}) {
    projectV2Field { ... on ProjectV2SingleSelectField { options { name } } }
  }
}"""


def parse_item(node: Dict[str, Any], status_field: str) -> Dict[str, Any]:
    """Turns a ProjectV2Item GraphQL node into the dict the commands use.

    Args:
        node: A node selected with ITEM_FRAGMENT.
        status_field: Name of the columns field.

    Returns:
        A dict with id, title, status, archived, fields (field name to
        value), assignees and content (with "type" in place of __typename).
    """
    content = dict(node.get("content") or {})
    content["type"] = content.pop("__typename", None)
    assignees = [a["login"] for a in
                 (content.pop("assignees", None) or {}).get("nodes", [])]
    values: Dict[str, Any] = {}
    for value in (node.get("fieldValues") or {}).get("nodes", []):
        name = ((value or {}).get("field") or {}).get("name")
        if not name or name == "Title":
            continue
        for key in ("name", "text", "number", "date", "title"):
            if key in value:
                values[name] = value[key]
                break
    status = next((v for k, v in values.items()
                   if k.lower() == status_field.lower()), None)
    return {"id": node["id"], "title": content.get("title", ""),
            "status": status, "archived": bool(node.get("isArchived")),
            "fields": values, "assignees": assignees, "content": content}


class Board:
    """One GitHub project, with its ID and fields cached per process."""

    def __init__(self, config: Config):
        self.config = config

    # -- GitHub access -----------------------------------------------------

    def graphql(self, query: str, **variables: Any) -> Dict[str, Any]:
        """Runs a GraphQL query through `gh api graphql` and returns data.

        Integers and None are sent as typed values (-F); strings as raw
        strings (-f), so a numeric-looking string stays a string.
        """
        args = ["api", "graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            if value is None:
                args += ["-F", f"{name}=null"]
            elif isinstance(value, int):
                args += ["-F", f"{name}={value}"]
            else:
                args += ["-f", f"{name}={value}"]
        out = json.loads(gh(*args))
        if out.get("errors"):
            raise BoardError(json.dumps(out["errors"]))
        return out["data"]

    def project_cmd(self, subcommand: str, *args: str) -> str:
        """Runs `gh project <subcommand> <number> --owner <owner> ...`."""
        return gh("project", subcommand, str(self.config.number),
                  "--owner", self.config.owner, *args)

    def _project_of(self, data: Dict[str, Any]) -> Dict[str, Any]:
        project = (data.get("repositoryOwner") or {}).get("projectV2")
        if not project:
            raise BoardError(f"no project {self.config.number} for owner "
                             f"{self.config.owner!r}, or no access to it")
        return project

    @functools.cached_property
    def _project(self) -> Dict[str, Any]:
        return self._project_of(self.graphql(
            PROJECT_QUERY, owner=self.config.owner,
            number=self.config.number))

    @property
    def project_id(self) -> str:
        """The project's PVT_ node ID."""
        return self._project["id"]

    @property
    def fields(self) -> List[Dict[str, Any]]:
        """Each field with its dataType (gh field-list omits the type)."""
        return [f for f in self._project["fields"]["nodes"] if f]

    def field(self, name: str) -> Dict[str, Any]:
        """Returns the field called `name` (case-insensitive)."""
        for f in self.fields:
            if f["name"].lower() == name.lower():
                return f
        raise BoardError(f"no field named {name!r}; see `board.py fields`")

    # -- Items ---------------------------------------------------------------

    def items(self, query: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lists the board's non-archived items, paginated, in board order.

        GitHub returns a freshly archived item for a short while, so items
        are filtered on isArchived. The listing can lag changes by about a
        minute.

        Args:
            query: Optional Projects filter, e.g. "priority:P0".

        Returns:
            Items as returned by parse_item.
        """
        text = ITEMS_QUERY % {
            "wrapper": OWNER_WRAPPER, "page": PAGE_SIZE,
            "qvar": ", $q: String!" if query else "",
            "qarg": ", query: $q" if query else ""}
        extra = {"q": query} if query else {}
        rows: List[Dict[str, Any]] = []
        cursor = None
        while len(rows) < ITEM_LIMIT:
            project = self._project_of(self.graphql(
                text, owner=self.config.owner, number=self.config.number,
                cursor=cursor, **extra))
            page = project["items"]
            rows += [parse_item(n, self.config.status_field)
                     for n in page["nodes"] if n]
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        return [r for r in rows if not r["archived"]]

    def item_by_id(self, node_id: str,
                   archived: bool = False) -> Dict[str, Any]:
        """Fetches one item of this board, with all its field values.

        Args:
            node_id: The item's PVTI_ node ID.
            archived: If True, require an archived item (for undo);
                otherwise refuse archived items.

        Raises:
            BoardError: If the item does not exist, belongs to another
                project, or has the wrong archived state.
        """
        node = self.graphql(ITEM_QUERY, id=node_id).get("node")
        if not node or "isArchived" not in node:
            raise BoardError(f"no project item {node_id}")
        if (node.get("project") or {}).get("id") != self.project_id:
            raise BoardError(f"{node_id} belongs to another project, not "
                             f"{self.config.owner}/{self.config.number}")
        item = parse_item(node, self.config.status_field)
        if item["archived"] and not archived:
            raise BoardError(f"{node_id} is archived; restore it first with "
                             f"`archive {node_id} --undo`")
        if archived and not item["archived"]:
            raise BoardError(f"{node_id} is not archived")
        return item

    def resolve(self, ref: str) -> Dict[str, Any]:
        """Finds exactly one item by node ID, number or title.

        Args:
            ref: A PVTI_ node ID, an issue/PR number ("#12" or "12"), or a
                case-insensitive title substring. A number that matches no
                issue or PR is matched as a title. Archived items never
                match.

        Returns:
            The matching item.

        Raises:
            BoardError: If no item or more than one item matches.
        """
        if ref.startswith("PVTI_"):
            return self.item_by_id(ref)
        all_items = self.items()
        hits: List[Dict[str, Any]] = []
        if ref.lstrip("#").isdigit():
            number = int(ref.lstrip("#"))
            hits = [i for i in all_items
                    if (i.get("content") or {}).get("number") == number]
        if not hits:
            needle = ref.lower()
            hits = [i for i in all_items if needle in i["title"].lower()]
            exact = [i for i in hits if i["title"].lower() == needle]
            if len(exact) == 1:
                hits = exact
        if len(hits) != 1:
            found = "\n".join(f"  {i['id']}  {i['title']}" for i in hits)
            raise BoardError(f"{len(hits)} items match {ref!r}"
                             + (f"; use the node ID:\n{found}" if hits else ""))
        return hits[0]

    def set_field(self, item_id: str, name: str, value: str) -> str:
        """Sets one field on one item; '' clears it.

        Returns:
            The value as the board names it (e.g. the option's real
            capitalization), or "" when cleared.

        Raises:
            BoardError: As for field_edit_args.
        """
        args, shown = self.field_edit_args(name, value)
        gh("project", "item-edit", "--id", item_id, *args)
        return shown

    def field_edit_args(self, name: str, value: str) -> Tuple[List[str], str]:
        """Validates a field value and builds its `gh project item-edit` flags.

        Makes no change on GitHub, so callers can validate before writing.
        The gh flag is chosen from the field's GraphQL dataType.

        Returns:
            The flags after `--id <item>`, and the value as the board names
            it ("" when cleared).

        Raises:
            BoardError: If the field, option or iteration does not exist, or
                the field type cannot be set with `gh project item-edit`.
        """
        f = self.field(name)
        kind = f["dataType"]
        if kind not in ("SINGLE_SELECT", "ITERATION", "TEXT", "NUMBER",
                        "DATE"):
            raise BoardError(f"{f['name']} ({kind}) cannot be set here; "
                             "edit the issue or PR with `gh issue edit`")
        args = ["--project-id", self.project_id, "--field-id", f["id"]]
        shown = value
        if value == "":
            args.append("--clear")
        elif kind == "SINGLE_SELECT":
            options = {o["name"].lower(): o for o in f["options"]}
            if value.lower() not in options:
                names = ", ".join(o["name"] for o in f["options"])
                raise BoardError(f"{f['name']} has no option {value!r}; "
                                 f"options: {names}")
            option = options[value.lower()]
            args += ["--single-select-option-id", option["id"]]
            shown = option["name"]
        elif kind == "ITERATION":
            iterations = (f.get("configuration") or {}).get("iterations", [])
            by_title = {i["title"].lower(): i for i in iterations}
            if value.lower() not in by_title:
                titles = ", ".join(i["title"] for i in iterations)
                raise BoardError(f"{f['name']} has no iteration {value!r}; "
                                 f"iterations: {titles}")
            args += ["--iteration-id", by_title[value.lower()]["id"]]
            shown = by_title[value.lower()]["title"]
        else:
            flag = {"NUMBER": "--number", "DATE": "--date"}.get(kind, "--text")
            args += [flag, value]
        return args, shown

    def add_url(self, url: str) -> str:
        """Adds an issue or PR to the board and returns the item ID."""
        out = self.project_cmd("item-add", "--url", url, "--format", "json")
        return json.loads(out)["id"]


# -- Option editing (pure, so it can be tested without gh) -----------------

def plan_options(f: Dict[str, Any], add: Optional[List[str]] = None,
                 rename: Optional[List[str]] = None,
                 remove: Optional[List[str]] = None,
                 order: Optional[str] = None) -> List[Dict[str, Any]]:
    """Computes a single-select field's new option list.

    Steps run in this order: rename, remove, add, order. Existing options
    keep their IDs, so cards keep their values through renames and reorders.
    New options have no ID and are colored GRAY.

    Args:
        f: The field as returned by Board.fields.
        add: Names of options to append.
        rename: "OLD=NEW" pairs.
        remove: Names of options to drop; the old or the new name of an
            option renamed in the same call both work.
        order: Comma-separated list of every resulting option name.

    Returns:
        The options payload for updateProjectV2Field.

    Raises:
        BoardError: On unknown names, duplicate names or an incomplete or
            repeated --order.
    """
    if f["dataType"] != "SINGLE_SELECT":
        raise BoardError(f"{f['name']} is not a single-select field")
    options = [{k: o.get(k) or "" for k in ("id", "name", "color",
                                              "description")}
               for o in f["options"]]
    original = {o["name"].lower(): opt for o, opt in zip(f["options"],
                                                         options)}

    def current_names() -> str:
        return ", ".join(o["name"] for o in options)

    def find(name: str) -> Optional[Dict[str, Any]]:
        for o in options:
            if o["name"].lower() == name.lower():
                return o
        o = original.get(name.lower())
        return o if o in options else None

    for pair in rename or []:
        old, _, new = pair.partition("=")
        option = find(old)
        if option is None or not new:
            raise BoardError(f"bad --rename {pair!r}; options: "
                             f"{current_names()}")
        clash = next((o for o in options if o is not option
                      and o["name"].lower() == new.lower()), None)
        if clash:
            raise BoardError(f"cannot rename {old!r} to {new!r}: an option "
                             "with that name already exists")
        option["name"] = new
    for name in remove or []:
        option = find(name)
        if option is None:
            raise BoardError(f"no option {name!r}; options: "
                             f"{current_names()}")
        options.remove(option)
    for name in add or []:
        if any(o["name"].lower() == name.lower() for o in options):
            raise BoardError(f"option {name!r} already exists")
        options.append({"name": name, "color": "GRAY", "description": ""})
    if order:
        wanted = [n.strip().lower() for n in order.split(",")]
        if (len(wanted) != len(set(wanted))
                or set(wanted) != {o["name"].lower() for o in options}):
            raise BoardError("--order must list every option exactly once; "
                             f"options: {current_names()}")
        rank = {n: k for k, n in enumerate(wanted)}
        options.sort(key=lambda o: rank[o["name"].lower()])
    return options


# -- Subcommands -------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> None:
    path = pathlib.Path.cwd() / CONFIG_FILENAME
    if path.exists() and not args.force:
        raise BoardError(f"{path} exists; pass --force to overwrite it")
    data: Dict[str, Any] = {"owner": args.owner, "number": args.number}
    if args.repo:
        data["repo"] = args.repo
    if args.init_status_field:
        data["status_field"] = args.init_status_field
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"wrote {path}")


def cmd_fields(board: Board, _args: argparse.Namespace) -> None:
    for f in board.fields:
        line = f"{f['name']}  ({f['dataType']})"
        if f.get("options"):
            line += ": " + " | ".join(o["name"] for o in f["options"])
        print(line)


def cmd_list(board: Board, args: argparse.Namespace) -> None:
    rows = board.items(args.query)
    if args.status:
        rows = [i for i in rows
                if (i.get("status") or "").lower() == args.status.lower()]
    options = board.field(board.config.status_field).get("options") or []
    rank = {o["name"].lower(): k for k, o in enumerate(options)}

    def column(item: Dict[str, Any]) -> Tuple[int, str]:
        status = item.get("status")
        if not status:
            return (len(rank) + 1, "")
        return (rank.get(status.lower(), len(rank)), status.lower())

    for i in sorted(rows, key=column):   # stable: board order within column
        c = i.get("content") or {}
        ref = f"#{c['number']}" if c.get("number") else "draft"
        who = ",".join(i.get("assignees") or [])
        print(f"{i.get('status') or '-':<14} {ref:<7} {i['title']}"
              f"{'  @' + who if who else ''}  [{i['id']}]")
    print(f"{len(rows)} items")


def cmd_show(board: Board, args: argparse.Namespace) -> None:
    i = board.resolve(args.item)
    c = i.get("content") or {}
    summary = {"id": i["id"], "title": i["title"], "type": c.get("type"),
               "status": i["status"], "archived": i["archived"],
               "assignees": i["assignees"], "fields": i["fields"]}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if c.get("url"):
        print(c["url"])
    print("\n" + (c.get("body") or "(no body)"))


def cmd_create(board: Board, args: argparse.Namespace) -> None:
    if args.status:    # validate before creating anything
        board.field_edit_args(board.config.status_field, args.status)
    if args.issue:
        if not board.config.repo:
            raise BoardError("create --issue needs a repository: set "
                             "\"repo\" in the same source as owner and "
                             "number (--repo, PROJECT_BOARD_REPO or the "
                             "config file)")
        url = gh("issue", "create", "--repo", board.config.repo,
                 "--title", args.title, "--body", args.body or "").strip()
        item_id = board.add_url(url)
        print(url)
    else:
        out = board.project_cmd("item-create", "--title", args.title,
                                "--body", args.body or "", "--format", "json")
        item_id = json.loads(out)["id"]
    if args.status:
        board.set_field(item_id, board.config.status_field, args.status)
    print(item_id)


def cmd_add(board: Board, args: argparse.Namespace) -> None:
    if args.status:    # validate before adding anything
        board.field_edit_args(board.config.status_field, args.status)
    item_id = board.add_url(args.url)
    if args.status:
        board.set_field(item_id, board.config.status_field, args.status)
    print(item_id)


def cmd_move(board: Board, args: argparse.Namespace) -> None:
    i = board.resolve(args.item)
    new = board.set_field(i["id"], board.config.status_field, args.status)
    print(f"{i['title']}: {i.get('status') or '-'} -> {new or '-'}")


def cmd_set(board: Board, args: argparse.Namespace) -> None:
    i = board.resolve(args.item)
    new = board.set_field(i["id"], args.field, args.value)
    print(f"{i['title']}: {board.field(args.field)['name']} = {new!r}")


def cmd_edit(board: Board, args: argparse.Namespace) -> None:
    """Edits a draft's title or body; issues use `gh issue edit`."""
    if args.title is None and args.body is None:
        raise BoardError("give --title, --body or both")
    if args.title is not None and not args.title.strip():
        raise BoardError("--title cannot be empty")
    i = board.resolve(args.item)
    c = i.get("content") or {}
    if c.get("type") != "DraftIssue":
        raise BoardError(f"not a draft; use: gh issue edit {c.get('url')}")
    cmd = ["project", "item-edit", "--id", c["id"]]
    if args.title is not None:
        cmd += ["--title", args.title]
    if args.body is not None:
        cmd += ["--body", args.body]
    gh(*cmd)
    print(f"edited {i['title']}  [{i['id']}]")


def cmd_archive(board: Board, args: argparse.Namespace) -> None:
    if args.undo:
        # Archived items drop out of GitHub's listing, so undo needs the
        # node ID that `archive` printed.
        if not args.item.startswith("PVTI_"):
            raise BoardError("archive --undo needs the PVTI_ node ID that "
                             "archive printed; archived items cannot be "
                             "found by title or number")
        i = board.item_by_id(args.item, archived=True)
        board.project_cmd("item-archive", "--id", i["id"], "--undo")
        print(f"unarchived: {i['title']}  [{i['id']}]")
        return
    i = board.resolve(args.item)
    board.project_cmd("item-archive", "--id", i["id"])
    print(f"archived: {i['title']}  [{i['id']}]")


def cmd_delete(board: Board, args: argparse.Namespace) -> None:
    i = board.resolve(args.item)
    board.project_cmd("item-delete", "--id", i["id"])
    print(f"deleted: {i['title']}  [{i['id']}]")


def cmd_options(board: Board, args: argparse.Namespace) -> None:
    """Adds, renames, removes or reorders a single-select field's options.

    Dry run unless --apply.
    """
    f = board.field(args.field)
    options = plan_options(f, add=args.add, rename=args.rename,
                           remove=args.remove, order=args.order)
    print("before: " + " | ".join(o["name"] for o in f["options"]))
    print("after:  " + " | ".join(o["name"] for o in options))
    if args.remove:
        print(f"cards in {', '.join(args.remove)} lose their "
              f"{f['name']} value")
    if not args.apply:
        print("dry run; add --apply to change the board")
        return
    body = json.dumps({"query": OPTIONS_MUTATION,
                       "variables": {"field": f["id"], "options": options}})
    out = json.loads(gh("api", "graphql", "--input", "-", stdin=body) or "{}")
    if out.get("errors"):
        raise BoardError(json.dumps(out["errors"]))
    print("applied")


# -- CLI -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Builds the argument parser with global board flags and subcommands."""
    p = argparse.ArgumentParser(
        description="Manage a GitHub Projects (v2) board through gh.",
        epilog="Board selection (one source supplies every key): flags > "
               "PROJECT_BOARD_* env vars > nearest .project-board.json > "
               "~/.config/project-board/config.json.")
    p.add_argument("--owner", dest="g_owner",
                   help="user or organization login that owns the project")
    p.add_argument("--number", dest="g_number", type=int,
                   help="project number")
    p.add_argument("--repo", dest="g_repo",
                   help="OWNER/NAME repository for create --issue")
    p.add_argument("--status-field", dest="g_status_field",
                   help=f"columns field (default {DEFAULT_STATUS_FIELD!r})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help=f"write {CONFIG_FILENAME} here")
    s.add_argument("owner")
    s.add_argument("number", type=int)
    s.add_argument("--repo")
    s.add_argument("--status-field", dest="init_status_field")
    s.add_argument("--force", action="store_true",
                   help="overwrite an existing file")
    sub.add_parser("fields", help="list fields, types and options")
    s = sub.add_parser("list", help="list items in board column order")
    s.add_argument("--status", help="only this column")
    s.add_argument("--query",
                   help='Projects filter, e.g. "priority:P0 -status:Done"')
    s = sub.add_parser("show", help="show one item's fields and body")
    s.add_argument("item")
    s = sub.add_parser("create", help="create a draft (or --issue) item")
    s.add_argument("title")
    s.add_argument("--body")
    s.add_argument("--status")
    s.add_argument("--issue", action="store_true",
                   help="create a real issue in the configured repo and add it")
    s = sub.add_parser("add", help="add an existing issue/PR by URL")
    s.add_argument("url")
    s.add_argument("--status")
    s = sub.add_parser("move", help="set an item's column (status field)")
    s.add_argument("item")
    s.add_argument("status")
    s = sub.add_parser("set", help="set any field ('' clears it)")
    s.add_argument("item")
    s.add_argument("field")
    s.add_argument("value")
    s = sub.add_parser("edit", help="edit a draft's title/body")
    s.add_argument("item")
    s.add_argument("--title")
    s.add_argument("--body")
    s = sub.add_parser("archive", help="archive an item, or --undo to "
                       "restore one by the PVTI_ ID that archive printed")
    s.add_argument("item")
    s.add_argument("--undo", action="store_true",
                   help="restore; ITEM must be the PVTI_ node ID")
    s = sub.add_parser("options", help="edit a single-select field's options "
                       "(columns); dry run unless --apply")
    s.add_argument("field")
    s.add_argument("--add", action="append", metavar="NAME")
    s.add_argument("--rename", action="append", metavar="OLD=NEW")
    s.add_argument("--remove", action="append", metavar="NAME")
    s.add_argument("--order", metavar="A,B,C")
    s.add_argument("--apply", action="store_true")
    s = sub.add_parser("delete", help="delete an item from the board")
    s.add_argument("item")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    """Runs the CLI and returns the process exit status."""
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "init":
            cmd_init(args)
            return 0
        flags = {"owner": args.g_owner, "number": args.g_number,
                 "repo": args.g_repo, "status_field": args.g_status_field}
        config = load_config(flags, os.environ, pathlib.Path.cwd(),
                             pathlib.Path.home())
        globals()[f"cmd_{args.cmd}"](Board(config), args)
    except BoardError as e:
        print(f"board.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
