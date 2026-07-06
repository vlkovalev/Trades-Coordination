import unittest

from app import create_app
from models import Project, db


class BlueprintRouteTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()

    def test_blueprint_route_renders_product_blueprint(self):
        response = self.client.get("/blueprint")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Construction Trade Coordination Platform", response.get_data(as_text=True))
        self.assertIn("MVP Scope", response.get_data(as_text=True))

    def test_dashboard_route_renders_operations_dashboard(self):
        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Operations Dashboard", response.get_data(as_text=True))
        self.assertIn("Today on Site", response.get_data(as_text=True))

    def test_project_creation_flow_works(self):
        response = self.client.post(
            "/projects/new",
            data={
                "name": "North ADU",
                "client_name": "Alex Rivera",
                "address": "123 Main St",
                "budget": "180000",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("North ADU", response.get_data(as_text=True))
        self.assertIn("Project created", response.get_data(as_text=True))

    def test_task_creation_flow_works(self):
        with self.app.app_context():
            project = Project(name="West Remodel", client_name="Jamie", address="456 Oak", budget=120000)
            db.session.add(project)
            db.session.commit()
            project_id = project.id

        response = self.client.post(
            f"/projects/{project_id}/tasks/new",
            data={"title": "Rough-in plumbing", "trade": "Plumbing", "status": "assigned"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Rough-in plumbing", response.get_data(as_text=True))
        self.assertIn("Plumbing", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
