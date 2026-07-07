import io
import unittest

from werkzeug.security import generate_password_hash

from app import create_app
from models import Assignment, ConsentRecord, Project, Task, TradeCompany, User, db


class TradeCoordinationRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()

            framing = TradeCompany(name="Rivera Framing Co.", trade_type="framing", phone="+15555550101")
            db.session.add(framing)
            db.session.add(
                TradeCompany(name="Bright Spark Electric", trade_type="electrical", phone="+15555550102")
            )
            db.session.flush()

            db.session.add(
                User(username="gc_test", password_hash=generate_password_hash("testpass123"), role="gc")
            )
            db.session.add(
                User(
                    username="rivera_test",
                    password_hash=generate_password_hash("testpass123"),
                    role="trade",
                    trade_company_id=framing.id,
                )
            )
            db.session.commit()

    def _login(self, username, password="testpass123"):
        return self.client.post(
            "/login", data={"username": username, "password": password}, follow_redirects=True
        )

    def test_blueprint_route_renders_product_blueprint(self):
        response = self.client.get("/blueprint")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Construction Trade Coordination Platform", response.get_data(as_text=True))
        self.assertIn("MVP Scope", response.get_data(as_text=True))

    def test_root_shows_landing_page_when_logged_out(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Nobody should find out a job is delayed", response.get_data(as_text=True))
        self.assertIn('href="/login"', response.get_data(as_text=True))

    def test_root_redirects_logged_in_user_to_their_dashboard(self):
        self._login("gc_test")
        response = self.client.get("/", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Operations Dashboard", response.get_data(as_text=True))

    def test_gc_dashboard_requires_login(self):
        response = self.client.get("/projects", follow_redirects=True)

        self.assertIn("log in", response.get_data(as_text=True).lower())

    def test_change_password_flow(self):
        self._login("gc_test")

        wrong_current = self.client.post(
            "/account/password",
            data={"current_password": "wrongpass", "new_password": "newpass1234", "confirm_password": "newpass1234"},
            follow_redirects=True,
        )
        self.assertIn("Current password is incorrect", wrong_current.get_data(as_text=True))

        mismatch = self.client.post(
            "/account/password",
            data={"current_password": "testpass123", "new_password": "newpass1234", "confirm_password": "different"},
            follow_redirects=True,
        )
        self.assertIn("do not match", mismatch.get_data(as_text=True))

        success = self.client.post(
            "/account/password",
            data={"current_password": "testpass123", "new_password": "newpass1234", "confirm_password": "newpass1234"},
            follow_redirects=True,
        )
        self.assertIn("Password updated", success.get_data(as_text=True))

        self.client.get("/logout")
        old_password_login = self._login("gc_test", password="testpass123")
        self.assertIn("Invalid username or password", old_password_login.get_data(as_text=True))

        new_password_login = self._login("gc_test", password="newpass1234")
        self.assertIn("Operations Dashboard", new_password_login.get_data(as_text=True))

    def test_project_creation_and_task_workflow(self):
        self._login("gc_test")

        response = self.client.post(
            "/projects/new",
            data={
                "name": "North ADU",
                "address": "123 Main St",
                "client_name": "Alex Rivera",
                "client_phone": "+15555559999",
                "consent": "yes",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Project created", response.get_data(as_text=True))

        with self.app.app_context():
            project = Project.query.filter_by(name="North ADU").first()
            project_id = project.id
            framing = TradeCompany.query.filter_by(trade_type="framing").first()
            trade_company_id = framing.id
            electrical = TradeCompany.query.filter_by(trade_type="electrical").first()
            electrical_id = electrical.id
            db.session.add(
                User(
                    username="homeowner_test",
                    password_hash=generate_password_hash("testpass123"),
                    role="homeowner",
                    project_id=project_id,
                )
            )
            db.session.commit()

        response = self.client.post(
            f"/projects/{project_id}/tasks/new",
            data={"name": "Framing", "trade_type_needed": "framing", "insert_after": ""},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Framing", response.get_data(as_text=True))

        with self.app.app_context():
            task = Task.query.filter_by(project_id=project_id).first()
            task_id = task.id

        response = self.client.post(
            f"/tasks/{task_id}/invite",
            data={"trade_company_id": trade_company_id},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("invited", response.get_data(as_text=True))

        with self.app.app_context():
            self.assertEqual(Task.query.get(task_id).status, "invited")
            assignment = Assignment.query.filter_by(task_id=task_id).first()
            assignment_id = assignment.id

        # Trade logs in and sees the invitation actually rendered on their queue
        # page (not just reflected in the DB) — regression test for a variable-
        # name mismatch that once made this page always render empty.
        self.client.get("/logout")
        self._login("rivera_test")
        queue_page = self.client.get("/trade/1")
        self.assertIn("Framing", queue_page.get_data(as_text=True))
        self.assertIn(f"/assignments/{assignment_id}/accept", queue_page.get_data(as_text=True))
        self.assertNotIn("No pending project invites", queue_page.get_data(as_text=True))
        self.client.post(f"/assignments/{assignment_id}/accept")
        self.client.post(f"/tasks/{task_id}/checkin")

        rejected = self.client.post(f"/tasks/{task_id}/complete", data={}, follow_redirects=True)
        self.assertIn("photo is required", rejected.get_data(as_text=True))

        completed = self.client.post(
            f"/tasks/{task_id}/complete",
            data={"photo": (io.BytesIO(b"fake-image-bytes"), "done.jpg")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("Task marked complete", completed.get_data(as_text=True))

        with self.app.app_context():
            self.assertEqual(Task.query.get(task_id).status, "complete")

        # Homeowner can review the trade that completed work on their project...
        self.client.get("/logout")
        self._login("homeowner_test")

        homeowner_page = self.client.get(f"/homeowner/{project_id}")
        self.assertIn("Rivera Framing Co.", homeowner_page.get_data(as_text=True))

        review = self.client.post(
            f"/projects/{project_id}/trades/{trade_company_id}/review",
            data={"rating": "5", "comment": "Great work, on time."},
            follow_redirects=True,
        )
        self.assertIn("Review saved", review.get_data(as_text=True))
        self.assertIn("you rated this 5/5", review.get_data(as_text=True))

        # ...but not a trade that never did any work on this project.
        ineligible = self.client.post(
            f"/projects/{project_id}/trades/{electrical_id}/review",
            data={"rating": "5", "comment": "n/a"},
            follow_redirects=True,
        )
        self.assertIn("only review a trade that has completed work", ineligible.get_data(as_text=True))

        with self.app.app_context():
            self.assertEqual(TradeCompany.query.get(trade_company_id).rating_display, "5.0★ (1 review)")

        # The rating shows up in the public directory on the home page too.
        self.client.get("/logout")
        directory = self.client.get("/")
        self.assertIn("5.0★ (1 review)", directory.get_data(as_text=True))

        # The audit log actually renders the entries that piled up above —
        # regression test for the same class of variable-name mismatch bug.
        self._login("gc_test")
        audit_page = self.client.get("/audit")
        self.assertIn("create_project", audit_page.get_data(as_text=True))
        self.assertIn("invite_trade", audit_page.get_data(as_text=True))
        self.assertNotIn("No system audits logged yet", audit_page.get_data(as_text=True))

    def test_readiness_blocks_checkin_until_prior_task_complete_or_overridden(self):
        self._login("gc_test")

        self.client.post(
            "/projects/new",
            data={
                "name": "West Wing",
                "address": "456 Oak",
                "client_name": "Jordan Lee",
                "client_phone": "+15555550111",
                "access_instructions": "Gate code 4321",
            },
            follow_redirects=True,
        )

        with self.app.app_context():
            project_id = Project.query.filter_by(name="West Wing").first().id
            framing_id = TradeCompany.query.filter_by(trade_type="framing").first().id
            electrical_id = TradeCompany.query.filter_by(trade_type="electrical").first().id

        # Framing task first, electrical depends on it.
        self.client.post(
            f"/projects/{project_id}/tasks/new",
            data={"name": "Framing", "trade_type_needed": "framing", "insert_after": ""},
        )
        with self.app.app_context():
            framing_task_id = Task.query.filter_by(project_id=project_id, name="Framing").first().id

        self.client.post(
            f"/projects/{project_id}/tasks/new",
            data={"name": "Rough-in Electrical", "trade_type_needed": "electrical", "insert_after": framing_task_id},
        )
        with self.app.app_context():
            electrical_task = Task.query.filter_by(project_id=project_id, name="Rough-in Electrical").first()
            electrical_task_id = electrical_task.id
            self.assertEqual(electrical_task.depends_on_task_id, framing_task_id)

        # Invite and confirm the electrical trade before framing is done.
        self.client.post(f"/tasks/{electrical_task_id}/invite", data={"trade_company_id": electrical_id})
        with self.app.app_context():
            electrical_assignment_id = Assignment.query.filter_by(task_id=electrical_task_id).first().id

        # Create an electrical trade login and accept the assignment.
        with self.app.app_context():
            db.session.add(
                User(
                    username="sparky_test",
                    password_hash=generate_password_hash("testpass123"),
                    role="trade",
                    trade_company_id=electrical_id,
                )
            )
            db.session.commit()

        self.client.get("/logout")
        self._login("sparky_test")
        self.client.post(f"/assignments/{electrical_assignment_id}/accept")

        blocked = self.client.post(f"/tasks/{electrical_task_id}/checkin", follow_redirects=True)
        self.assertIn("ready yet", blocked.get_data(as_text=True))
        self.assertIn("Framing", blocked.get_data(as_text=True))
        with self.app.app_context():
            # Accepted but not checked in — the readiness gate blocked the transition.
            self.assertEqual(Task.query.get(electrical_task_id).status, "confirmed")

        task_page = self.client.get(f"/tasks/{electrical_task_id}")
        self.assertIn("Not Ready", task_page.get_data(as_text=True))
        self.assertIn("check in is blocked", task_page.get_data(as_text=True))

        # GC overrides the readiness check.
        self.client.get("/logout")
        self._login("gc_test")
        override = self.client.post(
            f"/tasks/{electrical_task_id}/override-readiness",
            data={"reason": "Homeowner confirmed framing is actually done, system just hasn't caught up."},
            follow_redirects=True,
        )
        self.assertIn("Readiness override applied", override.get_data(as_text=True))

        # Now the electrical trade can check in.
        self.client.get("/logout")
        self._login("sparky_test")
        allowed = self.client.post(f"/tasks/{electrical_task_id}/checkin", follow_redirects=True)
        self.assertIn("Checked in", allowed.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(Task.query.get(electrical_task_id).status, "checked_in")

    def test_manage_trade_companies(self):
        self._login("gc_test")

        added = self.client.post(
            "/trades/new",
            data={"name": "Solid Roofing Co.", "trade_type": "general", "phone": "+15555550199", "service_area": "North side"},
            follow_redirects=True,
        )
        self.assertIn("Trade company added", added.get_data(as_text=True))
        self.assertIn("Solid Roofing Co.", added.get_data(as_text=True))

        with self.app.app_context():
            new_trade = TradeCompany.query.filter_by(name="Solid Roofing Co.").first()
            new_trade_id = new_trade.id
            framing_id = TradeCompany.query.filter_by(trade_type="framing").first().id

        edited = self.client.post(
            f"/trades/{new_trade_id}/edit",
            data={"name": "Solid Roofing LLC", "trade_type": "general", "phone": "+15555550199", "service_area": "North side"},
            follow_redirects=True,
        )
        self.assertIn("Trade company updated", edited.get_data(as_text=True))
        self.assertIn("Solid Roofing LLC", edited.get_data(as_text=True))

        # A trade with no assignments/reviews/logins can be deleted...
        deleted = self.client.post(f"/trades/{new_trade_id}/delete", follow_redirects=True)
        self.assertIn("removed", deleted.get_data(as_text=True))
        with self.app.app_context():
            self.assertIsNone(TradeCompany.query.get(new_trade_id))

        # ...but one with a login tied to it (rivera_test, seeded in setUp) cannot be.
        blocked = self.client.post(f"/trades/{framing_id}/delete", follow_redirects=True)
        self.assertIn("Can&#39;t delete".replace("&#39;", "'"), blocked.get_data(as_text=True).replace("&#39;", "'"))
        with self.app.app_context():
            self.assertIsNotNone(TradeCompany.query.get(framing_id))

    def test_seeding_does_not_recreate_deleted_trade_companies_on_restart(self):
        with self.app.app_context():
            for tc in TradeCompany.query.all():
                if tc.trade_type != "framing":
                    db.session.delete(tc)
            db.session.commit()
            remaining = TradeCompany.query.count()

        # Simulate the app restarting (create_app runs its one-time seed logic
        # again) — since a User already exists, it must NOT re-seed the trade
        # companies that were deliberately deleted.
        create_app()
        with self.app.app_context():
            self.assertEqual(TradeCompany.query.count(), remaining)

    def test_create_and_remove_trade_login(self):
        self._login("gc_test")

        with self.app.app_context():
            electrical_id = TradeCompany.query.filter_by(trade_type="electrical").first().id

        # No login yet, so it can be created.
        created = self.client.post(
            f"/trades/{electrical_id}/create-login",
            data={"username": "sparky_login", "password": "sparkypass123"},
            follow_redirects=True,
        )
        self.assertIn("Login created", created.get_data(as_text=True))

        # A second attempt is refused — one login per trade company.
        duplicate = self.client.post(
            f"/trades/{electrical_id}/create-login",
            data={"username": "someone_else", "password": "anotherpass123"},
            follow_redirects=True,
        )
        self.assertIn("already has a login", duplicate.get_data(as_text=True))

        # The new login actually works.
        self.client.get("/logout")
        login_check = self._login("sparky_login", password="sparkypass123")
        self.assertIn("Crew Operations Queue", login_check.get_data(as_text=True))
        self.client.get("/logout")

        # GC removes the login, freeing the trade company up again.
        self._login("gc_test")
        removed = self.client.post(f"/trades/{electrical_id}/remove-login", follow_redirects=True)
        self.assertIn("removed", removed.get_data(as_text=True))

        self.client.get("/logout")
        locked_out = self._login("sparky_login", password="sparkypass123")
        self.assertIn("Invalid username or password", locked_out.get_data(as_text=True))

    def test_delete_project_cascades_homeowner_login_but_blocks_with_tasks(self):
        self._login("gc_test")

        self.client.post(
            "/projects/new",
            data={
                "name": "Test Teardown Project",
                "client_name": "Alex",
                "client_phone": "+15555550188",
                "address": "789 Pine",
                "budget": "5000",
                "consent": "yes",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            project_id = Project.query.filter_by(name="Test Teardown Project").first().id
            # The normal, encouraged new_project flow also creates a ConsentRecord
            # (via the "consent" checkbox) -- confirm one was actually seeded here
            # so the deletion below exercises that cleanup, not just User/Review.
            self.assertIsNotNone(ConsentRecord.query.filter_by(project_id=project_id).first())
            db.session.add(
                User(
                    username="alex_homeowner",
                    password_hash=generate_password_hash("testpass123"),
                    role="homeowner",
                    project_id=project_id,
                )
            )
            db.session.commit()

        # A project with no tasks, but with a homeowner login and a consent
        # record tied to it, can still be deleted -- unlike a trade company's
        # login, a homeowner login is scoped 1:1 to its project and is
        # cascaded away with it (along with its consent records).
        deleted = self.client.post(f"/projects/{project_id}/delete", follow_redirects=True)
        self.assertIn("removed", deleted.get_data(as_text=True))
        with self.app.app_context():
            self.assertIsNone(Project.query.get(project_id))
            self.assertIsNone(User.query.filter_by(username="alex_homeowner").first())
            self.assertIsNone(ConsentRecord.query.filter_by(project_id=project_id).first())

        # A project with a task scheduled cannot be deleted.
        self.client.post(
            "/projects/new",
            data={
                "name": "Active Project",
                "client_name": "Sam",
                "client_phone": "+15555550177",
                "address": "12 Elm",
                "budget": "8000",
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            active_project_id = Project.query.filter_by(name="Active Project").first().id
            db.session.add(
                Task(project_id=active_project_id, trade_type_needed="framing", name="Framing", sequence_order=1)
            )
            db.session.commit()

        blocked = self.client.post(f"/projects/{active_project_id}/delete", follow_redirects=True)
        self.assertIn("Can&#39;t delete".replace("&#39;", "'"), blocked.get_data(as_text=True).replace("&#39;", "'"))
        with self.app.app_context():
            self.assertIsNotNone(Project.query.get(active_project_id))


if __name__ == "__main__":
    unittest.main()
