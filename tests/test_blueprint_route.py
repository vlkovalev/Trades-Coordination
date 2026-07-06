import io
import unittest

from werkzeug.security import generate_password_hash

from app import create_app
from models import Assignment, Project, Task, TradeCompany, User, db


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

        # Trade logs in, accepts, checks in, and completes with a required photo.
        self.client.get("/logout")
        self._login("rivera_test")
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


if __name__ == "__main__":
    unittest.main()
