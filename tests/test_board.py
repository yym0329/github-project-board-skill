"""Tests for board.py. `gh` is replaced by a fake; nothing touches the network.

Run from the repository root:
    python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]
                       / "skills" / "project-board"))
import board  # noqa: E402  pylint: disable=wrong-import-position

FIELDS = [
    {"id": "F_TITLE", "name": "Title", "dataType": "TITLE"},
    {"id": "F_STATUS", "name": "Status", "dataType": "SINGLE_SELECT",
     "options": [{"id": "o_backlog", "name": "Backlog", "color": "GRAY",
                  "description": ""},
                 {"id": "o_doing", "name": "In progress", "color": "YELLOW",
                  "description": "now"},
                 {"id": "o_done", "name": "Done", "color": "GREEN",
                  "description": ""}]},
    {"id": "F_STAGE", "name": "Stage", "dataType": "SINGLE_SELECT",
     "options": [{"id": "s_todo", "name": "Todo", "color": "GRAY",
                  "description": ""},
                 {"id": "s_ship", "name": "Shipped", "color": "BLUE",
                  "description": ""}]},
    {"id": "F_EST", "name": "Estimate", "dataType": "NUMBER"},
    {"id": "F_DUE", "name": "Target date", "dataType": "DATE"},
    {"id": "F_NOTES", "name": "Notes", "dataType": "TEXT"},
    {"id": "F_SPRINT", "name": "Sprint", "dataType": "ITERATION",
     "configuration": {"iterations": [{"id": "it1", "title": "Sprint 1",
                                       "startDate": "2026-01-01"}]}},
    {"id": "F_ASSIGN", "name": "Assignees", "dataType": "ASSIGNEES"},
]


def node(item_id, title, status=None, number=None, archived=False,
         values=None, body="", stage=None, assignees=(), project="PVT_1"):
    """Builds a ProjectV2Item node shaped like the GraphQL response."""
    field_values = [{"text": title, "field": {"name": "Title"}}, {}]
    if status:
        field_values.append({"name": status, "field": {"name": "Status"}})
    if stage:
        field_values.append({"name": stage, "field": {"name": "Stage"}})
    for name, value in (values or {}).items():
        key = {"Estimate": "number", "Target date": "date",
               "Sprint": "title"}.get(name, "text")
        field_values.append({key: value, "field": {"name": name}})
    if number is None:
        content = {"__typename": "DraftIssue", "id": "DI_" + item_id[5:],
                   "title": title, "body": body}
    else:
        content = {"__typename": "Issue", "id": "I_" + item_id[5:],
                   "title": title, "body": body, "number": number,
                   "url": f"https://github.com/octo/app/issues/{number}",
                   "assignees": {"nodes": [{"login": a} for a in assignees]}}
    return {"id": item_id, "isArchived": archived, "project": {"id": project},
            "fieldValues": {"nodes": field_values}, "content": content}


NODES = [
    node("PVTI_d", "Release checklist"),
    node("PVTI_b", "Fix login bug 2", "Done", 13),
    node("PVTI_a", "Fix login bug", "Backlog", 12, body="Goal: fix it.",
         values={"Estimate": 3, "Target date": "2026-12-01"},
         assignees=["ann"]),
    node("PVTI_c", "Write release notes", "In progress", body="Draft body"),
    node("PVTI_e", "2026", "Backlog"),
    # GitHub lists a freshly archived item only briefly; while it does, the
    # board must still hide it.
    node("PVTI_f", "Old release plan", "Done", 14, archived=True),
]

# Reachable by node ID only: archived and no longer listed, or on another
# project.
UNLISTED = [
    node("PVTI_g", "Stale idea", archived=True),
    node("PVTI_x", "Other board card", "Done", project="PVT_other"),
]


class FakeGh:
    """Records gh calls and answers the ones the board makes."""

    def __init__(self, nodes=None, page=3):
        self.calls = []
        self.nodes = NODES if nodes is None else nodes
        self.page = page

    def __call__(self, *args, stdin=None):
        self.calls.append((args, stdin))
        if args[:2] == ("api", "graphql") and stdin is None:
            query = next(a[6:] for a in args if a.startswith("query="))
            variables = dict(a.split("=", 1) for a in args[3:]
                             if not a.startswith("-") and "=" in a
                             and not a.startswith("query="))
            if "items(first" in query:
                return self._page(query, variables)
            if "repositoryOwner" in query:
                return json.dumps({"data": {"repositoryOwner": {"projectV2": {
                    "id": "PVT_1", "fields": {"nodes": FIELDS}}}}})
            if "node(id:" in query:
                found = next((n for n in self.nodes + UNLISTED
                              if n["id"] == variables["id"]), None)
                return json.dumps({"data": {"node": found}})
        if args[:2] == ("api", "graphql"):
            return json.dumps({"data": {}})
        if args[:2] == ("issue", "create"):
            return "https://github.com/octo/app/issues/99\n"
        if args[:2] in (("project", "item-create"), ("project", "item-add")):
            return json.dumps({"id": "PVTI_new"})
        return ""

    def _page(self, query, variables):
        cursor = variables.get("cursor", "null")
        start = 0 if cursor == "null" else int(cursor)
        chunk = self.nodes[start:start + self.page]
        more = start + self.page < len(self.nodes)
        project = {"items": {"nodes": chunk, "pageInfo": {
                       "hasNextPage": more,
                       "endCursor": str(start + self.page)}}}
        if "field(name:" in query:
            # Like GitHub, field(name:) matches the exact name only.
            options = next((f["options"] for f in FIELDS
                            if f["name"] == variables["status"]), [])
            project["field"] = {"options": [{"name": o["name"]}
                                            for o in options]}
        return json.dumps({"data": {"repositoryOwner": {
            "projectV2": project}}})

    def by_prefix(self, *prefix):
        return [a for a, _ in self.calls if a[:len(prefix)] == prefix]

    def graphql_queries(self):
        return [next(a for a in args if a.startswith("query="))
                for args in self.by_prefix("api", "graphql")
                if any(a.startswith("query=") for a in args)]


class BoardTestCase(unittest.TestCase):
    """Sets up a Board for octo/7 with a fake gh."""

    status_field = "Status"
    repo = "octo/app"

    def setUp(self):
        self.fake = FakeGh()
        patcher = mock.patch.object(board, "gh", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.board = board.Board(board.Config(
            owner="octo", number=7, repo=self.repo,
            status_field=self.status_field))

    def run_cmd(self, argv):
        """Runs one subcommand and returns what it printed."""
        args = board.build_parser().parse_args(argv)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            getattr(board, f"cmd_{args.cmd}")(self.board, args)
        return out.getvalue()


class ConfigTest(unittest.TestCase):

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.root = self.tmp / "work"
        self.deep = self.root / "a" / "b"
        self.deep.mkdir(parents=True)
        (self.home / ".config" / "project-board").mkdir(parents=True)

    def write(self, path, data):
        path.write_text(json.dumps(data))

    def user_file(self, data):
        self.write(self.home / ".config" / "project-board" / "config.json",
                   data)

    def load(self, flags=None, env=None):
        return board.load_config(flags or {}, env or {}, self.deep, self.home)

    def test_flags_beat_everything(self):
        self.user_file({"owner": "user", "number": 1})
        self.write(self.deep / ".project-board.json",
                   {"owner": "file", "number": 2})
        cfg = self.load(flags={"owner": "f", "number": 5},
                        env={"PROJECT_BOARD_OWNER": "e",
                             "PROJECT_BOARD_NUMBER": "6"})
        self.assertEqual((cfg.owner, cfg.number, cfg.source),
                         ("f", 5, "flags"))

    def test_env_beats_files(self):
        self.write(self.deep / ".project-board.json",
                   {"owner": "file", "number": 2})
        cfg = self.load(env={"PROJECT_BOARD_OWNER": "env",
                             "PROJECT_BOARD_NUMBER": "4",
                             "PROJECT_BOARD_REPO": ""})
        self.assertEqual((cfg.owner, cfg.number), ("env", 4))

    def test_nearest_project_file_beats_farther_and_user(self):
        self.user_file({"owner": "user", "number": 1})
        self.write(self.root / ".project-board.json",
                   {"owner": "far", "number": 2})
        self.write(self.root / "a" / ".project-board.json",
                   {"owner": "near", "number": 3})
        cfg = self.load()
        self.assertEqual((cfg.owner, cfg.number), ("near", 3))
        self.assertEqual(cfg.status_field, "Status")

    def test_user_file_used_when_nothing_else(self):
        self.user_file({"owner": "user", "number": "8", "repo": "u/r"})
        cfg = self.load()
        self.assertEqual((cfg.owner, cfg.number, cfg.repo),
                         ("user", 8, "u/r"))

    def test_keys_are_not_mixed_across_sources(self):
        self.write(self.deep / ".project-board.json",
                   {"owner": "file", "number": 2, "repo": "file/r",
                    "status_field": "Stage"})
        cfg = self.load(flags={"owner": "other-org", "number": 9})
        self.assertIsNone(cfg.repo)
        self.assertEqual(cfg.status_field, "Status")

    def test_partial_source_is_an_error(self):
        self.write(self.deep / ".project-board.json",
                   {"owner": "file", "number": 2})
        with self.assertRaises(board.BoardError) as ctx:
            self.load(flags={"owner": "other-org"})
        self.assertIn("--number", str(ctx.exception))
        with self.assertRaises(board.BoardError) as ctx:
            self.load(env={"PROJECT_BOARD_NUMBER": "3"})
        self.assertIn("PROJECT_BOARD_OWNER", str(ctx.exception))
        with self.assertRaises(board.BoardError):
            self.load(flags={"repo": "x/y"})
        self.write(self.deep / ".project-board.json", {"owner": "file"})
        self.user_file({"owner": "user", "number": 1})
        with self.assertRaises(board.BoardError):
            self.load()

    def test_missing_config_names_every_option(self):
        with self.assertRaises(board.BoardError) as ctx:
            self.load()
        msg = str(ctx.exception)
        for hint in ("--owner", "--number", "PROJECT_BOARD_OWNER",
                     "PROJECT_BOARD_NUMBER", ".project-board.json",
                     "~/.config/project-board/config.json", "init"):
            self.assertIn(hint, msg)

    def test_bad_number_and_unknown_key(self):
        with self.assertRaises(board.BoardError):
            self.load(flags={"owner": "o", "number": "seven"})
        self.write(self.deep / ".project-board.json",
                   {"owner": "o", "number": 1, "nubmer": 2})
        with self.assertRaises(board.BoardError):
            self.load()

    def test_broken_lower_file_does_not_block_flags_or_env(self):
        self.write(self.deep / ".project-board.json",
                   {"owner": "o", "number": 1, "nubmer": 2})
        (self.home / ".config" / "project-board" / "config.json").write_text(
            "{not json")
        cfg = self.load(flags={"owner": "f", "number": 7})
        self.assertEqual((cfg.owner, cfg.number), ("f", 7))
        cfg = self.load(env={"PROJECT_BOARD_OWNER": "e",
                             "PROJECT_BOARD_NUMBER": "8"})
        self.assertEqual((cfg.owner, cfg.number), ("e", 8))
        # A good project file shields a broken user file, too.
        self.write(self.deep / ".project-board.json",
                   {"owner": "p", "number": 2})
        self.assertEqual(self.load().owner, "p")

    def test_init_writes_loadable_file(self):
        args = board.build_parser().parse_args(
            ["init", "acme", "3", "--repo", "acme/web"])
        with mock.patch.object(board.pathlib.Path, "cwd",
                               return_value=self.deep), \
                contextlib.redirect_stdout(io.StringIO()):
            board.cmd_init(args)
            with self.assertRaises(board.BoardError):
                board.cmd_init(args)          # refuses to overwrite
        cfg = self.load()
        self.assertEqual((cfg.owner, cfg.number, cfg.repo),
                         ("acme", 3, "acme/web"))


class ItemsTest(BoardTestCase):

    def test_archived_items_are_excluded_and_pages_followed(self):
        ids = [i["id"] for i in self.board.items()]
        self.assertEqual(ids, ["PVTI_d", "PVTI_b", "PVTI_a", "PVTI_c",
                               "PVTI_e"])
        self.assertEqual(len(self.fake.by_prefix("api", "graphql")), 2)

    def test_query_uses_owner_union_and_typed_variables(self):
        self.board.items(query="priority:P0")
        args = self.fake.by_prefix("api", "graphql")[0]
        query = next(a for a in args if a.startswith("query="))
        self.assertIn("repositoryOwner(login: $owner)", query)
        self.assertIn("... on Organization", query)
        self.assertIn("query: $q", query)
        self.assertIn("isArchived", query)
        self.assertEqual(args[args.index("q=priority:P0") - 1], "-f")
        self.assertEqual(args[args.index("number=7") - 1], "-F")
        self.assertEqual(args[args.index("cursor=null") - 1], "-F")

    def test_no_query_variable_without_filter(self):
        self.board.items()
        self.assertNotIn("$q", self.fake.graphql_queries()[0])

    def test_list_in_column_order_without_archived(self):
        lines = self.run_cmd(["list"]).splitlines()
        self.assertEqual([line.split("[")[-1].rstrip("]") for line in
                          lines[:-1]],
                         ["PVTI_a", "PVTI_e", "PVTI_c", "PVTI_b", "PVTI_d"])
        self.assertIn("#12", lines[0])
        self.assertIn("@ann", lines[0])
        self.assertEqual(lines[-1], "5 items")

    def test_list_status_filter(self):
        out = self.run_cmd(["list", "--status", "backlog"])
        self.assertEqual(out.count("PVTI_"), 2)

    def test_list_has_no_archived_option(self):
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            board.build_parser().parse_args(["list", "--archived"])


class CustomStatusFieldTest(BoardTestCase):
    status_field = "Stage"

    def test_status_read_from_configured_field(self):
        self.fake.nodes = [node("PVTI_x", "x", status="Done", stage="Shipped")]
        item = self.board.items()[0]
        self.assertEqual(item["status"], "Shipped")

    def test_move_uses_configured_field_and_prints_canonical_name(self):
        out = self.run_cmd(["move", "#12", "shipped"])
        edit = list(self.fake.by_prefix("project", "item-edit")[0])
        self.assertIn("F_STAGE", edit)
        self.assertEqual(edit[-2:], ["--single-select-option-id", "s_ship"])
        self.assertEqual(out.strip(), "Fix login bug: - -> Shipped")


class LowercaseStatusFieldTest(BoardTestCase):
    status_field = "status"

    def test_list_still_sorts_by_column_order(self):
        lines = self.run_cmd(["list"]).splitlines()
        self.assertEqual([line.split("[")[-1].rstrip("]") for line in
                          lines[:-1]],
                         ["PVTI_a", "PVTI_e", "PVTI_c", "PVTI_b", "PVTI_d"])

    def test_set_prints_board_spelling_of_field(self):
        out = self.run_cmd(["set", "PVTI_a", "target DATE", "2026-12-24"])
        self.assertEqual(out.strip(),
                         "Fix login bug: Target date = '2026-12-24'")


class ResolveTest(BoardTestCase):

    def test_node_id_uses_small_queries_and_no_listing(self):
        item = self.board.resolve("PVTI_a")
        self.assertEqual((item["id"], item["title"], item["status"]),
                         ("PVTI_a", "Fix login bug", "Backlog"))
        self.assertEqual(item["fields"]["Estimate"], 3)
        queries = self.fake.graphql_queries()
        self.assertEqual(len(queries), 2)     # the item, the project's ID
        self.assertFalse(any("items(first" in q for q in queries))

    def test_node_id_of_other_project_is_refused(self):
        with self.assertRaises(board.BoardError) as ctx:
            self.board.resolve("PVTI_x")
        self.assertIn("another project", str(ctx.exception))

    def test_archived_node_id_is_refused_outside_undo(self):
        for argv in (["show", "PVTI_g"], ["move", "PVTI_g", "Done"],
                     ["delete", "PVTI_g"], ["archive", "PVTI_g"]):
            with self.assertRaises(board.BoardError, msg=argv) as ctx:
                self.run_cmd(argv)
            self.assertIn("--undo", str(ctx.exception))
        self.assertEqual(self.fake.by_prefix("project"), [])

    def test_unknown_node_id(self):
        with self.assertRaises(board.BoardError):
            self.board.resolve("PVTI_nope")

    def test_number_with_and_without_hash(self):
        self.assertEqual(self.board.resolve("#13")["id"], "PVTI_b")
        self.assertEqual(self.board.resolve("12")["id"], "PVTI_a")

    def test_digit_title_falls_back_to_title_match(self):
        self.assertEqual(self.board.resolve("2026")["id"], "PVTI_e")

    def test_unique_title_substring_case_insensitive(self):
        self.assertEqual(self.board.resolve("NOTES")["id"], "PVTI_c")

    def test_exact_title_wins_over_substring(self):
        self.assertEqual(self.board.resolve("fix login bug")["id"], "PVTI_a")

    def test_ambiguous_title_lists_candidates(self):
        with self.assertRaises(board.BoardError) as ctx:
            self.board.resolve("release")
        msg = str(ctx.exception)
        self.assertIn("2 items match", msg)
        self.assertIn("PVTI_c", msg)
        self.assertIn("PVTI_d", msg)
        self.assertNotIn("PVTI_f", msg)     # archived

    def test_archived_items_not_found_by_title_or_number(self):
        for ref in ("Old release plan", "#14", "stale"):
            with self.assertRaises(board.BoardError, msg=ref):
                self.board.resolve(ref)


class ShowTest(BoardTestCase):

    def test_node_id_and_title_print_the_same(self):
        by_id = self.run_cmd(["show", "PVTI_a"])
        by_title = self.run_cmd(["show", "Fix login bug"])
        self.assertEqual(by_id, by_title)
        summary = json.loads(by_id.split("\n}\n")[0] + "\n}")
        self.assertEqual(summary["fields"], {"Status": "Backlog",
                                             "Estimate": 3,
                                             "Target date": "2026-12-01"})
        self.assertEqual(summary["assignees"], ["ann"])
        self.assertIn("https://github.com/octo/app/issues/12", by_id)
        self.assertTrue(by_id.rstrip().endswith("Goal: fix it."))


class SetFieldTest(BoardTestCase):

    def edit_args(self):
        calls = self.fake.by_prefix("project", "item-edit")
        self.assertEqual(len(calls), 1)
        return list(calls[0])

    def base(self, field_id):
        return ["project", "item-edit", "--id", "PVTI_a",
                "--project-id", "PVT_1", "--field-id", field_id]

    def test_single_select_case_insensitive(self):
        shown = self.board.set_field("PVTI_a", "status", "in PROGRESS")
        self.assertEqual(self.edit_args(), self.base("F_STATUS")
                         + ["--single-select-option-id", "o_doing"])
        self.assertEqual(shown, "In progress")

    def test_number(self):
        self.board.set_field("PVTI_a", "Estimate", "3.5")
        self.assertEqual(self.edit_args(),
                         self.base("F_EST") + ["--number", "3.5"])

    def test_date(self):
        self.board.set_field("PVTI_a", "Target date", "2026-12-01")
        self.assertEqual(self.edit_args(),
                         self.base("F_DUE") + ["--date", "2026-12-01"])

    def test_text(self):
        self.board.set_field("PVTI_a", "Notes", "42")
        self.assertEqual(self.edit_args(),
                         self.base("F_NOTES") + ["--text", "42"])

    def test_iteration(self):
        self.board.set_field("PVTI_a", "Sprint", "sprint 1")
        self.assertEqual(self.edit_args(),
                         self.base("F_SPRINT") + ["--iteration-id", "it1"])

    def test_empty_value_clears_any_type(self):
        for name, fid in (("Status", "F_STATUS"), ("Estimate", "F_EST"),
                          ("Notes", "F_NOTES")):
            self.fake.calls.clear()
            self.board.set_field("PVTI_a", name, "")
            self.assertEqual(self.edit_args(), self.base(fid) + ["--clear"])

    def test_unknown_option_and_uneditable_type_send_nothing(self):
        with self.assertRaises(board.BoardError) as ctx:
            self.board.set_field("PVTI_a", "Status", "Blocked")
        self.assertIn("Backlog, In progress, Done", str(ctx.exception))
        with self.assertRaises(board.BoardError):
            self.board.set_field("PVTI_a", "Assignees", "me")
        with self.assertRaises(board.BoardError):
            self.board.set_field("PVTI_a", "Nope", "x")
        self.assertEqual(self.fake.by_prefix("project", "item-edit"), [])

    def test_project_and_fields_fetched_once(self):
        self.board.set_field("PVTI_a", "Status", "Done")
        self.board.set_field("PVTI_a", "Estimate", "1")
        queries = self.fake.graphql_queries()
        self.assertEqual(len(queries), 1)
        self.assertIn("repositoryOwner(login: $owner)", queries[0])
        self.assertIn("... on User", queries[0])
        self.assertIn("... on Organization", queries[0])
        self.assertNotIn("user(login:", queries[0])

    def test_move_prints_canonical_option_name(self):
        out = self.run_cmd(["move", "PVTI_a", "in progress"])
        self.assertEqual(out.strip(), "Fix login bug: Backlog -> In progress")


class WriteCommandsTest(BoardTestCase):

    def test_edit_targets_draft_content_id_with_given_flags_only(self):
        self.run_cmd(["edit", "release notes", "--title", "New title"])
        self.assertEqual(self.fake.by_prefix("project", "item-edit"),
                         [("project", "item-edit", "--id", "DI_c",
                           "--title", "New title")])
        self.fake.calls.clear()
        self.run_cmd(["edit", "release notes", "--body", ""])
        self.assertEqual(self.fake.by_prefix("project", "item-edit"),
                         [("project", "item-edit", "--id", "DI_c",
                           "--body", "")])

    def test_edit_refuses_non_draft_empty_title_and_no_flags(self):
        for argv in (["edit", "#12", "--title", "X"],
                     ["edit", "release notes", "--title", ""],
                     ["edit", "release notes"]):
            with self.assertRaises(board.BoardError, msg=argv):
                self.run_cmd(argv)
        self.assertEqual(self.fake.by_prefix("project", "item-edit"), [])

    def test_create_draft_with_status(self):
        out = self.run_cmd(["create", "New card", "--body", "Goal: x",
                            "--status", "done"])
        self.assertEqual(self.fake.by_prefix("project", "item-create"),
                         [("project", "item-create", "7", "--owner", "octo",
                           "--title", "New card", "--body", "Goal: x",
                           "--format", "json")])
        edit = self.fake.by_prefix("project", "item-edit")[0]
        self.assertEqual(edit[3], "PVTI_new")
        self.assertEqual(edit[-1], "o_done")
        self.assertEqual(out.strip(), "PVTI_new")

    def test_create_issue_uses_repo_then_adds_url(self):
        self.run_cmd(["create", "Bug", "--issue"])
        self.assertEqual(self.fake.by_prefix("issue", "create"),
                         [("issue", "create", "--repo", "octo/app",
                           "--title", "Bug", "--body", "")])
        self.assertEqual(self.fake.by_prefix("project", "item-add"),
                         [("project", "item-add", "7", "--owner", "octo",
                           "--url", "https://github.com/octo/app/issues/99",
                           "--format", "json")])
        self.assertEqual(self.fake.by_prefix("project", "item-edit"), [])

    def test_bad_status_creates_nothing(self):
        for argv in (["create", "New card", "--status", "Nope"],
                     ["create", "Bug", "--issue", "--status", "Nope"],
                     ["add", "https://github.com/octo/app/issues/5",
                      "--status", "Nope"]):
            with self.assertRaises(board.BoardError, msg=argv):
                self.run_cmd(argv)
        self.assertEqual(self.fake.by_prefix("project", "item-create"), [])
        self.assertEqual(self.fake.by_prefix("project", "item-add"), [])
        self.assertEqual(self.fake.by_prefix("issue"), [])

    def test_archive_by_title_and_undo_by_node_id(self):
        out = self.run_cmd(["archive", "release notes"])
        self.assertIn("[PVTI_c]", out)
        self.run_cmd(["archive", "PVTI_g", "--undo"])
        self.assertEqual(self.fake.by_prefix("project", "item-archive"), [
            ("project", "item-archive", "7", "--owner", "octo",
             "--id", "PVTI_c"),
            ("project", "item-archive", "7", "--owner", "octo",
             "--id", "PVTI_g", "--undo"),
        ])

    def test_undo_needs_archived_node_id(self):
        with self.assertRaises(board.BoardError) as ctx:
            self.run_cmd(["archive", "Stale idea", "--undo"])
        self.assertIn("PVTI_", str(ctx.exception))
        for ref in ("PVTI_c", "PVTI_x"):    # not archived; other project
            with self.assertRaises(board.BoardError, msg=ref):
                self.run_cmd(["archive", ref, "--undo"])
        self.assertEqual(self.fake.by_prefix("project", "item-archive"), [])

    def test_delete(self):
        out = self.run_cmd(["delete", "#13"])
        self.assertEqual(self.fake.by_prefix("project", "item-delete"),
                         [("project", "item-delete", "7", "--owner", "octo",
                           "--id", "PVTI_b")])
        self.assertIn("Fix login bug 2", out)


class NoRepoTest(BoardTestCase):
    repo = None

    def test_create_issue_without_repo_fails_before_any_call(self):
        with self.assertRaises(board.BoardError):
            self.run_cmd(["create", "Bug", "--issue"])
        self.assertEqual(self.fake.calls, [])


class OptionsTest(BoardTestCase):

    def mutations(self):
        return [(a, s) for a, s in self.fake.calls
                if a[:2] == ("api", "graphql") and s is not None]

    def status(self):
        return self.board.field("Status")

    def test_dry_run_sends_no_mutation(self):
        self.run_cmd(["options", "Status", "--add", "In review",
                      "--rename", "Done=Shipped"])
        self.assertEqual(self.mutations(), [])

    def test_apply_keeps_ids_and_metadata(self):
        self.run_cmd(["options", "Status", "--add", "In review",
                      "--rename", "Done=Shipped", "--remove", "Backlog",
                      "--order", "In review,In progress,Shipped", "--apply"])
        (args, stdin), = self.mutations()
        self.assertEqual(args, ("api", "graphql", "--input", "-"))
        payload = json.loads(stdin)
        self.assertIn("updateProjectV2Field", payload["query"])
        self.assertEqual(payload["variables"]["field"], "F_STATUS")
        self.assertEqual(payload["variables"]["options"], [
            {"name": "In review", "color": "GRAY", "description": ""},
            {"id": "o_doing", "name": "In progress", "color": "YELLOW",
             "description": "now"},
            {"id": "o_done", "name": "Shipped", "color": "GREEN",
             "description": ""},
        ])

    def test_rename_onto_existing_name_is_refused(self):
        with self.assertRaises(board.BoardError):
            board.plan_options(self.status(), rename=["Backlog=done"])
        with self.assertRaises(board.BoardError):
            board.plan_options(self.status(),
                               rename=["Backlog=Later", "Done=later"])
        # Freed by an earlier rename, so this one is allowed.
        opts = board.plan_options(self.status(),
                                  rename=["Done=Shipped", "Backlog=Done"])
        self.assertEqual([(o["id"], o["name"]) for o in opts],
                         [("o_backlog", "Done"), ("o_doing", "In progress"),
                          ("o_done", "Shipped")])

    def test_order_refuses_duplicates(self):
        with self.assertRaises(board.BoardError):
            board.plan_options(self.status(),
                               order="Backlog,In progress,Done,Done")

    def test_remove_accepts_old_or_new_name_after_rename(self):
        for name in ("Shipped", "Done"):
            opts = board.plan_options(self.status(), rename=["Done=Shipped"],
                                      remove=[name])
            self.assertEqual([o["id"] for o in opts],
                             ["o_backlog", "o_doing"])

    def test_plan_rejects_bad_requests(self):
        for kwargs in ({"rename": ["Nope=X"]}, {"rename": ["Done="]},
                       {"remove": ["Nope"]}, {"add": ["done"]},
                       {"order": "Backlog,Done"}):
            with self.assertRaises(board.BoardError, msg=kwargs):
                board.plan_options(self.status(), **kwargs)
        with self.assertRaises(board.BoardError):
            board.plan_options(self.board.field("Notes"))

    def test_plan_does_not_mutate_cached_field(self):
        board.plan_options(self.status(), rename=["Done=Shipped"])
        self.assertEqual(self.status()["options"][2]["name"], "Done")


class GhHelperTest(unittest.TestCase):

    def test_failure_raises_with_stderr(self):
        failed = subprocess.CompletedProcess(["gh"], 1, "", "missing scope\n")
        with mock.patch.object(board.subprocess, "run", return_value=failed):
            with self.assertRaises(board.BoardError) as ctx:
                board.gh("project", "list")
        self.assertEqual(str(ctx.exception), "missing scope")

    def test_stdin_is_passed(self):
        ok = subprocess.CompletedProcess(["gh"], 0, "out", "")
        with mock.patch.object(board.subprocess, "run",
                               return_value=ok) as run:
            self.assertEqual(board.gh("api", stdin="body"), "out")
        self.assertEqual(run.call_args.args[0], ["gh", "api"])
        self.assertEqual(run.call_args.kwargs["input"], "body")


if __name__ == "__main__":
    unittest.main()
