"""Publishing regression checks in an isolated SQLite database, never the live library."""

import os
os.environ["DATABASE_URL"] = "sqlite://"

from copy import deepcopy
from pathlib import Path
import unittest

from fastapi import HTTPException
from starlette.requests import Request

from app import main
from app.database import Base, SessionLocal, engine
from app.models import Puzzle, User
from app.publishing import (
    IssueWrite, api_create_issue, api_get_issue, api_update_issue,
    api_latest_issue, api_latest_issue_pdf, issue_pdf, print_issue, publish_issue, write_issue,
)


def request(user_id=1, size="zebra"):
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                    "query_string": f"size={size}".encode(),
                    "session": {"user_id": user_id, "session_version": 1}})


class PublishingChecks(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(engine)
        self.db = SessionLocal()
        self.admin = User(id=1, display_name="Test admin", is_admin=True, is_active=True, session_version=1)
        self.player = User(id=2, display_name="Test player", is_admin=False, is_active=True, session_version=1)
        self.db.add_all([self.admin, self.player])
        self.puzzle = Puzzle(id="testwordsearch", title="First test puzzle", puzzle_type="word_search",
                             theme="test", difficulty="easy", prompt="Find the words.",
                             print_payload={"kind": "word_search", "grid": [list("CAT"), list("ABC"), list("DEF")],
                                            "words": ["CAT"], "settings": {}},
                             play_payload={}, solution_payload={}, puzzle_metadata={})
        self.db.add(self.puzzle)
        self.db.commit()
        self.payload = IssueWrite(title="Test issue", modules=[
            {"kind": "puzzle", "puzzle_id": self.puzzle.id},
            {"kind": "text", "title": "A daily note", "body": "A second section <script>alert(1)</script>", "attribution": "Test source"},
        ])

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(engine)

    def test_draft_permissions_and_publish_permissions(self):
        issue = write_issue(self.db, self.admin, self.payload)
        with self.assertRaises(HTTPException) as denied:
            api_create_issue(self.payload, request(2), self.db)
        self.assertEqual(denied.exception.status_code, 403)
        with self.assertRaises(HTTPException) as hidden:
            api_get_issue(issue.id, request(2), self.db)
        self.assertEqual(hidden.exception.status_code, 404)
        publish_issue(self.db, issue, request())
        result = api_get_issue(issue.id, request(2), self.db)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["modules"][0]["puzzle_id"], self.puzzle.id)
        self.assertNotIn("html", result["modules"][0])
        with self.assertRaises(HTTPException) as immutable:
            api_update_issue(issue.id, self.payload, request(), self.db)
        self.assertEqual(immutable.exception.status_code, 409)
        issue.archived_at = main.utcnow()
        self.db.commit()
        with self.assertRaises(HTTPException) as archived:
            api_get_issue(issue.id, request(2), self.db)
        self.assertEqual(archived.exception.status_code, 404)

    def test_snapshot_survives_source_changes_and_deletion(self):
        issue = write_issue(self.db, self.admin, self.payload)
        publish_issue(self.db, issue, request())
        snapshot = deepcopy(issue.snapshot)
        self.puzzle.title = "Changed source title"
        self.puzzle.print_payload = {"kind": "word_search", "grid": [], "words": []}
        self.db.commit()
        self.db.delete(self.puzzle)
        self.db.commit()
        self.db.expire_all()
        output = print_issue(issue, request(), self.db, self.admin, toolbar=False).body.decode()
        self.assertIn("First test puzzle", output)
        self.assertNotIn("Changed source title", output)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", output)
        self.assertEqual(issue.snapshot, snapshot)
        self.assertLess(output.index("First test puzzle"), output.index("A daily note"))

    def test_invalid_modules_and_empty_publication(self):
        with self.assertRaises(HTTPException) as missing:
            write_issue(self.db, self.admin, IssueWrite(title="Missing", modules=[{"puzzle_id": "missing"}]))
        self.assertEqual(missing.exception.status_code, 422)
        issue = write_issue(self.db, self.admin, IssueWrite(title="Empty"))
        with self.assertRaises(HTTPException) as empty:
            publish_issue(self.db, issue, request())
        self.assertEqual(empty.exception.status_code, 422)
        self.puzzle.puzzle_metadata = {"draft": True}
        self.db.commit()
        issue = write_issue(self.db, self.admin, self.payload)
        with self.assertRaises(HTTPException) as unpublished:
            publish_issue(self.db, issue, request())
        self.assertEqual(unpublished.exception.status_code, 422)

    def test_combined_pdf(self):
        issue = write_issue(self.db, self.admin, self.payload)
        publish_issue(self.db, issue, request())
        output = Path("/tmp/puzzary-publishing-qa")
        output.mkdir(exist_ok=True)
        for size in ("zebra", "letter", "a4"):
            response = issue_pdf(issue, request(size=size), self.db, self.admin)
            self.assertTrue(response.body.startswith(b"%PDF-"))
            (output / f"bundle-{size}.pdf").write_bytes(response.body)

    def test_latest_and_api_token_access(self):
        first = write_issue(self.db, self.admin, self.payload)
        publish_issue(self.db, first, request())
        second = write_issue(self.db, self.admin, IssueWrite(title="Second issue", modules=self.payload.modules))
        publish_issue(self.db, second, request())
        self.player.api_token_hash = main.hash_secret("mock-publishing-token")
        self.db.commit()
        token_request = Request({"type": "http", "method": "GET", "path": "/", "session": {},
                                 "query_string": b"size=zebra", "headers": [(b"authorization", b"Bearer mock-publishing-token")]})
        self.assertEqual(api_latest_issue(token_request, self.db)["id"], second.id)
        self.assertTrue(api_latest_issue_pdf(token_request, self.db).body.startswith(b"%PDF-"))
        second.archived_at = main.utcnow()
        self.db.commit()
        self.assertEqual(api_latest_issue(token_request, self.db)["id"], first.id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
