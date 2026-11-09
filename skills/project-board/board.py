#!/usr/bin/env python3
"""Manage a GitHub Projects (v2) board from the command line through `gh`.

Stdlib only. Every subcommand shells out to the GitHub CLI, which needs the
`project` token scope (`gh auth refresh -h github.com -s project`).

The board is chosen by, highest priority first: the global flags --owner,
--number, --repo and --status-field; the environment variables
PROJECT_BOARD_OWNER, PROJECT_BOARD_NUMBER, PROJECT_BOARD_REPO and
PROJECT_BOARD_STATUS_FIELD; the nearest .project-board.json found by searching
upward from the current directory; and ~/.config/project-board/config.json.
`board.py init OWNER NUMBER` writes .project-board.json in the current
directory.

Items can be named by node ID (PVTI_...), by issue or pull request number
(#12 or 12), or by a case-insensitive title substring that matches exactly one
item (an exact title match wins over substring matches).
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
from typing import Any, Dict, List, Mapping, Optional

CONFIG_FILENAME = ".project-board.json"
USER_CONFIG = pathlib.Path(".config", "project-board", "config.json")
ENV_PREFIX = "PROJECT_BOARD_"
CONFIG_KEYS = ("owner", "number", "repo", "status_field")
DEFAULT_STATUS_FIELD = "Status"
ITEM_LIMIT = 1000

MISSING_CONFIG_HELP = """\
No project board configured (missing: {missing}).
Set the board in one of these ways, highest priority first:
  flags:        board.py --owner LOGIN --number N [--repo OWNER/NAME] ...
  environment:  PROJECT_BOARD_OWNER, PROJECT_BOARD_NUMBER,
                PROJECT_BOARD_REPO, PROJECT_BOARD_STATUS_FIELD
  project file: .project-board.json in this directory or a parent
                (create it with: board.py init LOGIN N [--repo OWNER/NAME])
  user file:    ~/.config/project-board/config.json
Both files hold JSON such as {{"owner": "octocat", "number": 1}}."""


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
    """

    owner: str
    number: int
    repo: Optional[str] = None
    status_field: str = DEFAULT_STATUS_FIELD


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
    """Resolves the board configuration key by key.

    Args:
        flags: Values from the global CLI flags; None means not given.
        env: Environment variables (normally os.environ).
        cwd: Directory where the upward search for .project-board.json starts.
        home: Home directory holding .config/project-board/config.json.

    Returns:
        The merged configuration. Each key comes from the highest-priority
        source that sets it: flags, then env, then the nearest project file,
        then the user file.

    Raises:
        BoardError: If owner or number is missing or invalid, or a config
            file is unreadable.
    """
    layers: List[Mapping[str, Any]] = [
        flags,
        {key: env.get(ENV_PREFIX + key.upper()) for key in CONFIG_KEYS},
    ]
    project_file = find_project_config(cwd)
    if project_file:
        layers.append(_read_config_file(project_file))
    user_file = home / USER_CONFIG
    if user_file.is_file():
        layers.append(_read_config_file(user_file))

    merged: Dict[str, Any] = {}
    for key in CONFIG_KEYS:
        for layer in layers:
            value = layer.get(key)
            if value not in (None, ""):
                merged[key] = value
                break
    missing = [k for k in ("owner", "number") if k not in merged]
    if missing:
        raise BoardError(MISSING_CONFIG_HELP.format(missing=", ".join(missing)))
    try:
        number = int(merged["number"])
    except (TypeError, ValueError):
        raise BoardError(f"project number must be an integer, "
                         f"got {merged['number']!r}") from None
    return Config(owner=str(merged["owner"]), number=number,
                  repo=merged.get("repo"),
                  status_field=merged.get("status_field",
                                          DEFAULT_STATUS_FIELD))


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


# The project may belong to a user or an organization, so the query goes
# through repositoryOwner and asks both concrete types.
PROJECT_QUERY = """
query($owner: String!, $number: Int!) {
  repositoryOwner(login: $owner) {
    ... on User { projectV2(number: $number) { ...board } }
    ... on Organization { projectV2(number: $number) { ...board } }
  }
}
fragment board on ProjectV2 {
  id
  fields(first: 100) { nodes {
    ... on ProjectV2FieldCommon { id name dataType }
    ... on ProjectV2SingleSelectField { options { id name color description } }
    ... on ProjectV2IterationField {
      configuration { iterations { id title startDate } } }
  } }
}"""

ITEM_QUERY = """
query($id: ID!, $status: String!) { node(id: $id) { ... on ProjectV2Item {
  id
  status: fieldValueByName(name: $status) {
    ... on ProjectV2ItemFieldSingleSelectValue { name } }
  content {
    __typename
    ... on DraftIssue { id title body }
    ... on Issue { id title body number url }
    ... on PullRequest { id title body number url }
  } } } }"""

OPTIONS_MUTATION = """
mutation($field: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]) {
  updateProjectV2Field(input: {fieldId: $field, singleSelectOptions: $options}) {
    projectV2Field { ... on ProjectV2SingleSelectField { options { name } } }
  }
}"""


def _key(name: str) -> str:
    """Normalizes a field name for comparison (case and spaces ignored)."""
    return name.replace(" ", "").lower()


class Board:
    """One GitHub project, with its ID and fields cached per process."""

    def __init__(self, config: Config):
        self.config = config
        self._owner_args = ("--owner", config.owner)

    # -- GitHub access -----------------------------------------------------

    def graphql(self, query: str, **variables: Any) -> Dict[str, Any]:
        """Runs a GraphQL query through `gh api graphql` and returns data."""
        args = ["api", "graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            flag = "-F" if isinstance(value, int) else "-f"
            args += [flag, f"{name}={value}"]
        out = json.loads(gh(*args))
        if out.get("errors"):
            raise BoardError(json.dumps(out["errors"]))
        return out["data"]

    def project_cmd(self, subcommand: str, *args: str) -> str:
        """Runs `gh project <subcommand> <number> --owner <owner> ...`."""
        return gh("project", subcommand, str(self.config.number),
                  *self._owner_args, *args)

    @functools.cached_property
    def _project(self) -> Dict[str, Any]:
        data = self.graphql(PROJECT_QUERY, owner=self.config.owner,
                            number=self.config.number)
        owner = data.get("repositoryOwner")
        project = (owner or {}).get("projectV2")
        if not project:
            raise BoardError(f"no project {self.config.number} for owner "
                             f"{self.config.owner!r}, or no access to it")
        return project

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
        """Lists non-archived items; the API cannot list archived ones."""
        args = ["--limit", str(ITEM_LIMIT), "--format", "json"]
        if query:
            args += ["--query", query]
        rows = json.loads(self.project_cmd("item-list", *args))["items"]
        return [self._normalize(row) for row in rows]

    def _normalize(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Puts the status column under "status" for any status field name."""
        status_key = _key(self.config.status_field)
        row = dict(row)
        row["status"] = next((v for k, v in row.items()
                              if _key(k) == status_key), None)
        return row

    def item_by_id(self, node_id: str) -> Dict[str, Any]:
        """Fetches one item by node ID with one small query."""
        node = self.graphql(ITEM_QUERY, id=node_id,
                            status=self.config.status_field).get("node")
        if not node:
            raise BoardError(f"no item {node_id}")
        content = dict(node.get("content") or {})
        content["type"] = content.pop("__typename", None)
        return {"id": node["id"], "title": content.get("title", ""),
                "status": (node.get("status") or {}).get("name"),
                "content": content}

    def resolve(self, ref: str) -> Dict[str, Any]:
        """Finds exactly one item by node ID, number or title.

        Args:
            ref: A PVTI_ node ID, an issue/PR number ("#12" or "12"), or a
                case-insensitive title substring.

        Returns:
            The matching item.

        Raises:
            BoardError: If no item or more than one item matches.
        """
        if ref.startswith("PVTI_"):
            return self.item_by_id(ref)
        all_items = self.items()
        if ref.lstrip("#").isdigit():
            number = int(ref.lstrip("#"))
            hits = [i for i in all_items
                    if (i.get("content") or {}).get("number") == number]
        else:
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

    def set_field(self, item_id: str, name: str, value: str) -> None:
        """Sets one field on one item; '' clears it.

        The gh flag is chosen from the field's GraphQL dataType.

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
        args = ["project", "item-edit", "--id", item_id,
                "--project-id", self.project_id, "--field-id", f["id"]]
        if value == "":
            args.append("--clear")
        elif kind == "SINGLE_SELECT":
            options = {o["name"].lower(): o["id"] for o in f["options"]}
            if value.lower() not in options:
                names = ", ".join(o["name"] for o in f["options"])
                raise BoardError(f"{f['name']} has no option {value!r}; "
                                 f"options: {names}")
            args += ["--single-select-option-id", options[value.lower()]]
        elif kind == "ITERATION":
            iterations = (f.get("configuration") or {}).get("iterations", [])
            ids = {i["title"].lower(): i["id"] for i in iterations}
            if value.lower() not in ids:
                titles = ", ".join(i["title"] for i in iterations)
                raise BoardError(f"{f['name']} has no iteration {value!r}; "
                                 f"iterations: {titles}")
            args += ["--iteration-id", ids[value.lower()]]
        else:
            flag = {"NUMBER": "--number", "DATE": "--date"}.get(kind, "--text")
            args += [flag, value]
        gh(*args)

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

    Existing options keep their IDs, so cards keep their values through
    renames and reorders. New options have no ID and are colored GRAY.

    Args:
        f: The field as returned by Board.fields.
        add: Names of options to append.
        rename: "OLD=NEW" pairs.
        remove: Names of options to drop.
        order: Comma-separated list of every resulting option name.

    Returns:
        The options payload for updateProjectV2Field.

    Raises:
        BoardError: On unknown names, duplicates or an incomplete --order.
    """
    if f["dataType"] != "SINGLE_SELECT":
        raise BoardError(f"{f['name']} is not a single-select field")
    options = [{k: o.get(k) or "" for k in ("id", "name", "color",
                                              "description")}
               for o in f["options"]]
    by_name = {o["name"].lower(): o for o in options}
    names = ", ".join(o["name"] for o in options)
    for pair in rename or []:
        old, _, new = pair.partition("=")
        if old.lower() not in by_name or not new:
            raise BoardError(f"bad --rename {pair!r}; options: {names}")
        by_name[old.lower()]["name"] = new
    for name in remove or []:
        if name.lower() not in by_name:
            raise BoardError(f"no option {name!r}; options: {names}")
        options.remove(by_name.pop(name.lower()))
    for name in add or []:
        if any(o["name"].lower() == name.lower() for o in options):
            raise BoardError(f"option {name!r} already exists")
        options.append({"name": name, "color": "GRAY", "description": ""})
    if order:
        rank = {n.strip().lower(): k for k, n in enumerate(order.split(","))}
        if set(rank) != {o["name"].lower() for o in options}:
            raise BoardError("--order must list every option exactly once")
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
    for i in sorted(rows, key=lambda i: i.get("status") or ""):
        c = i.get("content") or {}
        ref = f"#{c['number']}" if c.get("number") else "draft"
        who = ",".join(i.get("assignees") or [])
        print(f"{i.get('status') or '-':<14} {ref:<7} {i['title']}"
              f"{'  @' + who if who else ''}  [{i['id']}]")
    print(f"{len(rows)} items")


def cmd_show(board: Board, args: argparse.Namespace) -> None:
    i = board.resolve(args.item)
    c = i.get("content") or {}
    print(json.dumps({k: v for k, v in i.items() if k != "content"},
                     indent=2, ensure_ascii=False))
    if c.get("url"):
        print(c["url"])
    print("\n" + (c.get("body") or "(no body)"))


def cmd_create(board: Board, args: argparse.Namespace) -> None:
    if args.issue:
        if not board.config.repo:
            raise BoardError("create --issue needs a repository: set --repo, "
                             "PROJECT_BOARD_REPO or \"repo\" in the config")
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
    item_id = board.add_url(args.url)
    if args.status:
        board.set_field(item_id, board.config.status_field, args.status)
    print(item_id)


def cmd_move(board: Board, args: argparse.Namespace) -> None:
    i = board.resolve(args.item)
    board.set_field(i["id"], board.config.status_field, args.status)
    print(f"{i['title']}: {i.get('status') or '-'} -> {args.status}")


def cmd_set(board: Board, args: argparse.Namespace) -> None:
    i = board.resolve(args.item)
    board.set_field(i["id"], args.field, args.value)
    print(f"{i['title']}: {args.field} = {args.value!r}")


def cmd_edit(board: Board, args: argparse.Namespace) -> None:
    """Edits a draft's title or body; issues use `gh issue edit`."""
    i = board.resolve(args.item)
    c = i.get("content") or {}
    if c.get("type") != "DraftIssue":
        raise BoardError(f"not a draft; use: gh issue edit {c.get('url')}")
    if args.title is None and args.body is None:
        raise BoardError("give --title, --body or both")
    cmd = ["project", "item-edit", "--id", c["id"]]
    if args.title:
        cmd += ["--title", args.title]
    if args.body is not None:
        cmd += ["--body", args.body]
    gh(*cmd)
    print(f"edited {i['id']}")


def cmd_archive(board: Board, args: argparse.Namespace) -> None:
    if args.undo:
        # Archived items are invisible to item-list, so undo needs the node
        # ID that `archive` printed.
        if not args.item.startswith("PVTI_"):
            raise BoardError("archive --undo needs the PVTI_ node ID that "
                             "archive printed")
        board.project_cmd("item-archive", "--id", args.item, "--undo")
        print(f"unarchived: {args.item}")
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
        epilog="Board selection: flags > PROJECT_BOARD_* env vars > nearest "
               ".project-board.json > ~/.config/project-board/config.json.")
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
    s = sub.add_parser("list", help="list items, grouped by status")
    s.add_argument("--status", help="only this column")
    s.add_argument("--query",
                   help='Projects filter, e.g. "priority:P0 -status:Done"')
    s = sub.add_parser("show", help="show one item and its body")
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
    s = sub.add_parser("archive", help="archive (or --undo) an item")
    s.add_argument("item")
    s.add_argument("--undo", action="store_true")
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
