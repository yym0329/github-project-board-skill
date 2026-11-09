"""Tests for board.py. `gh` is replaced by a fake; nothing touches the network.

Run from the repository root:
    python3 -m unittest discover -s tests -v
"""

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

ITEMS = [
    {"id": "PVTI_a", "title": "Fix login bug", "status": "Backlog",
     "content": {"type": "Issue", "number": 12, "title": "Fix login bug"}},
    {"id": "PVTI_b", "title": "Fix login bug 2", "status": "Done",
     "content": {"type": "Issue", "number": 13, "title": "Fix login bug 2"}},
    {"id": "PVTI_c", "title": "Write release notes", "status": "In progress",
     "content": {"type": "DraftIssue", "id": "DI_c",
                 "title": "Write release notes"}},
    {"id": "PVTI_d", "title": "Release checklist", "status": None,
     "content": {"type": "DraftIssue", "id": "DI_d",
                 "title": "Release checklist"}},
]


class FakeGh:
    """Records gh calls and answers the few the board makes."""

    def __init__(self, items=None, project_owner_type="User"):
        self.calls = []
        self.items = ITEMS if items is None else items
        self.project_owner_type = project_owner_type

    def __call__(self, *args, stdin=None):
        self.calls.append((args, stdin))
        if args[:2] == ("api", "graphql") and stdin is None:
            query = next(a for a in args if a.startswith("query="))
            if "repositoryOwner" in query:
                project = {"id": "PVT_1", "fields": {"nodes": FIELDS}}
                return json.dumps({"data": {"repositoryOwner": {
                    "projectV2": project}}})
            if "node(id:" in query:
                return json.dumps({"data": {"node": {
                    "id": "PVTI_z", "status": {"name": "Done"},
                    "content": {"__typename": "Issue", "id": "I_z",
                                "title": "From node", "number": 99}}}})
        if args[:2] == ("api", "graphql"):
            return json.dumps({"data": {}})
        if args[:2] == ("project", "item-list"):
            return json.dumps({"items": self.items,
                               "totalCount": len(self.items)})
        return "{}"

    def by_prefix(self, *prefix):
        return [a for a, _ in self.calls if a[:len(prefix)] == prefix]


def make_board(status_field="Status", items=None):
    fake = FakeGh(items=items)
    patcher = mock.patch.object(board, "gh", fake)
    patcher.start()
    config = board.Config(owner="octo", number=7, repo="octo/app",
                          status_field=status_field)
    return board.Board(config), fake, patcher


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

    def load(self, flags=None, env=None, cwd=None):
        return board.load_config(flags or {}, env or {}, cwd or self.deep,
                                 self.home)

    def test_precedence_key_by_key(self):
        self.user_file({"owner": "user-o", "number": 1, "repo": "u/r",
                        "status_field": "UserStage"})
        self.write(self.root / ".project-board.json",
                   {"owner": "far-o", "number": 2, "repo": "far/r"})
        self.write(self.root / "a" / ".project-board.json",
                   {"owner": "near-o", "number": 3})
        env = {"PROJECT_BOARD_NUMBER": "4", "PROJECT_BOARD_REPO": "",
               "PROJECT_BOARD_STATUS_FIELD": None}
        cfg = self.load(flags={"owner": "flag-o", "number": None}, env=env)
        self.assertEqual(cfg.owner, "flag-o")          # flag
        self.assertEqual(cfg.number, 4)                # env beats files
        # The nearest file has no repo, and an empty env var does not count;
        # the user file supplies it, not the farther project file.
        self.assertEqual(cfg.repo, "u/r")
        self.assertEqual(cfg.status_field, "UserStage")

    def test_flag_beats_env(self):
        cfg = self.load(flags={"owner": "f", "number": 5},
                        env={"PROJECT_BOARD_OWNER": "e",
                             "PROJECT_BOARD_NUMBER": "6"})
        self.assertEqual((cfg.owner, cfg.number), ("f", 5))

    def test_env_beats_project_file(self):
        self.write(self.root / ".project-board.json",
                   {"owner": "file", "number": 2})
        cfg = self.load(env={"PROJECT_BOARD_OWNER": "env"})
        self.assertEqual((cfg.owner, cfg.number), ("env", 2))

    def test_nearest_project_file_beats_farther_and_user(self):
        self.user_file({"owner": "user", "number": 1})
        self.write(self.root / ".project-board.json",
                   {"owner": "far", "number": 2})
        self.write(self.deep / ".project-board.json",
                   {"owner": "near", "number": 3})
        cfg = self.load()
        self.assertEqual((cfg.owner, cfg.number), ("near", 3))
        self.assertEqual(cfg.status_field, "Status")

    def test_user_file_used_when_nothing_else(self):
        self.user_file({"owner": "user", "number": "8"})
        cfg = self.load()
        self.assertEqual((cfg.owner, cfg.number, cfg.repo),
                         ("user", 8, None))

    def test_missing_config_names_every_option(self):
        with self.assertRaises(board.BoardError) as ctx:
            self.load(env={"PROJECT_BOARD_OWNER": "only-owner"})
        msg = str(ctx.exception)
        self.assertIn("missing: number", msg)
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

    def test_init_writes_loadable_file(self):
        args = board.build_parser().parse_args(
            ["init", "acme", "3", "--repo", "acme/web"])
        with mock.patch.object(board.pathlib.Path, "cwd",
                               return_value=self.deep), \
                mock.patch("sys.stdout"):
            board.cmd_init(args)
            with self.assertRaises(board.BoardError):
                board.cmd_init(args)          # refuses to overwrite
        cfg = self.load()
        self.assertEqual((cfg.owner, cfg.number, cfg.repo),
                         ("acme", 3, "acme/web"))


class ResolveTest(unittest.TestCase):

    def setUp(self):
        self.board, self.fake, patcher = make_board()
        self.addCleanup(patcher.stop)

    def test_node_id_uses_one_node_query_and_no_listing(self):
        item = self.board.resolve("PVTI_z")
        self.assertEqual((item["id"], item["title"], item["status"]),
                         ("PVTI_z", "From node", "Done"))
        self.assertEqual(item["content"]["type"], "Issue")
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.fake.by_prefix("project", "item-list"), [])
        args = self.fake.calls[0][0]
        self.assertIn("status=Status", args)

    def test_number_with_and_without_hash(self):
        self.assertEqual(self.board.resolve("#13")["id"], "PVTI_b")
        self.assertEqual(self.board.resolve("12")["id"], "PVTI_a")

    def test_unique_title_substring_case_insensitive(self):
        self.assertEqual(self.board.resolve("NOTES")["id"], "PVTI_c")
        self.assertEqual(len(self.fake.by_prefix("project", "item-list")), 1)

    def test_exact_title_wins_over_substring(self):
        self.assertEqual(self.board.resolve("fix login bug")["id"], "PVTI_a")

    def test_ambiguous_title_lists_candidates(self):
        with self.assertRaises(board.BoardError) as ctx:
            self.board.resolve("release")
        msg = str(ctx.exception)
        self.assertIn("2 items match", msg)
        self.assertIn("PVTI_c", msg)
        self.assertIn("PVTI_d", msg)

    def test_no_match(self):
        with self.assertRaises(board.BoardError) as ctx:
            self.board.resolve("#404")
        self.assertIn("0 items match", str(ctx.exception))

    def test_list_reads_custom_status_field(self):
        b, _, patcher = make_board(
            status_field="Stage",
            items=[{"id": "PVTI_x", "title": "x", "stage": "Shipped",
                    "status": "stale"}])
        self.addCleanup(patcher.stop)
        self.assertEqual(b.items()[0]["status"], "Shipped")


class SetFieldTest(unittest.TestCase):

    def setUp(self):
        self.board, self.fake, patcher = make_board()
        self.addCleanup(patcher.stop)

    def edit_args(self):
        calls = self.fake.by_prefix("project", "item-edit")
        self.assertEqual(len(calls), 1)
        return list(calls[0])

    def base(self, field_id):
        return ["project", "item-edit", "--id", "PVTI_a",
                "--project-id", "PVT_1", "--field-id", field_id]

    def test_single_select_case_insensitive(self):
        self.board.set_field("PVTI_a", "status", "in PROGRESS")
        self.assertEqual(self.edit_args(), self.base("F_STATUS")
                         + ["--single-select-option-id", "o_doing"])

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
        graphql = self.fake.by_prefix("api", "graphql")
        self.assertEqual(len(graphql), 1)
        query = next(a for a in graphql[0] if a.startswith("query="))
        self.assertIn("repositoryOwner(login: $owner)", query)
        self.assertIn("... on User", query)
        self.assertIn("... on Organization", query)
        self.assertNotIn("user(login:", query)
        self.assertIn("number=7", graphql[0])
        self.assertEqual(graphql[0][graphql[0].index("number=7") - 1], "-F")

    def test_move_uses_configured_status_field(self):
        b, fake, patcher = make_board(status_field="Stage")
        self.addCleanup(patcher.stop)
        args = board.build_parser().parse_args(["move", "#12", "shipped"])
        with mock.patch("sys.stdout"):
            board.cmd_move(b, args)
        edit = list(fake.by_prefix("project", "item-edit")[0])
        self.assertIn("F_STAGE", edit)
        self.assertEqual(edit[-2:], ["--single-select-option-id", "s_ship"])


class OptionsTest(unittest.TestCase):

    def setUp(self):
        self.board, self.fake, patcher = make_board()
        self.addCleanup(patcher.stop)

    def run_options(self, argv):
        args = board.build_parser().parse_args(["options", "Status"] + argv)
        with mock.patch("sys.stdout"):
            board.cmd_options(self.board, args)

    def mutations(self):
        return [(a, s) for a, s in self.fake.calls
                if a[:2] == ("api", "graphql") and s is not None]

    def test_dry_run_sends_no_mutation(self):
        self.run_options(["--add", "In review", "--rename", "Done=Shipped"])
        self.assertEqual(self.mutations(), [])

    def test_apply_keeps_ids_and_metadata(self):
        self.run_options(["--add", "In review", "--rename", "Done=Shipped",
                          "--remove", "Backlog",
                          "--order", "In review,In progress,Shipped",
                          "--apply"])
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

    def test_plan_rejects_bad_requests(self):
        status = self.board.field("Status")
        for kwargs in ({"rename": ["Nope=X"]}, {"rename": ["Done="]},
                       {"remove": ["Nope"]}, {"add": ["done"]},
                       {"order": "Backlog,Done"}):
            with self.assertRaises(board.BoardError, msg=kwargs):
                board.plan_options(status, **kwargs)
        with self.assertRaises(board.BoardError):
            board.plan_options(self.board.field("Notes"))

    def test_plan_does_not_mutate_cached_field(self):
        status = self.board.field("Status")
        board.plan_options(status, rename=["Done=Shipped"])
        self.assertEqual(status["options"][2]["name"], "Done")


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
