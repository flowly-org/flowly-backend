from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APITestCase

from tasks.models import Attachment
from workspaces.models import Membership

WITH_BUCKET = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    "attachments": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
}


class DiscussionTests(APITestCase):
    def setUp(self):
        self.alice = self.client.post("/api/auth/register/", {"email": "alice@example.com", "password": "s3cure-Passw0rd"}).data
        self.bob = self.client.post("/api/auth/register/", {"email": "bob@example.com", "password": "s3cure-Passw0rd"}).data
        self.as_user(self.alice)
        self.ws = self.client.post("/api/workspaces/", {"name": "Team"}).data
        Membership.objects.create(workspace_id=self.ws["id"], user_id=self.bob["user"]["id"])
        self.task = self.client.post(f"/api/workspaces/{self.ws['id']}/tasks/", {"title": "T"}, format="json").data
        self.base = f"/api/workspaces/{self.ws['id']}/tasks/{self.task['id']}"

    def as_user(self, auth):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth['access']}")

    def test_comments_permissions_and_counts(self):
        c = self.client.post(f"{self.base}/comments/", {"body": "  First!  "}, format="json").data
        self.assertEqual((c["body"], c["author"]["email"]), ("First!", "alice@example.com"))
        self.assertEqual(self.client.post(f"{self.base}/comments/", {"body": "   "}, format="json").status_code, 400)

        self.as_user(self.bob)
        self.client.post(f"{self.base}/comments/", {"body": "Reply"}, format="json")
        self.assertEqual(self.client.patch(f"{self.base}/comments/{c['id']}/", {"body": "hacked"}, format="json").status_code, 403)
        self.assertEqual(self.client.delete(f"{self.base}/comments/{c['id']}/").status_code, 403)

        self.as_user(self.alice)
        res = self.client.patch(f"{self.base}/comments/{c['id']}/", {"body": "Edited"}, format="json")
        self.assertIsNotNone(res.data["edited_at"])
        self.assertEqual([x["body"] for x in self.client.get(f"{self.base}/comments/").data], ["Edited", "Reply"])
        tasks = self.client.get(f"/api/workspaces/{self.ws['id']}/tasks/").data
        self.assertEqual((tasks[0]["comment_count"], tasks[0]["attachment_count"]), (2, 0))
        self.assertEqual(self.client.get("/api/my/tasks/").data[0]["comment_count"], 2)

    def test_links_and_cloud_documents(self):
        res = self.client.post(
            f"{self.base}/attachments/",
            {"kind": "google_drive", "name": "Spec", "url": "https://docs.google.com/document/d/abc", "meta": {"mimeType": "doc", "nested": {"x": 1}}},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["meta"], {"mimeType": "doc"})
        res = self.client.post(f"{self.base}/attachments/", {"kind": "link", "url": "https://example.com/a/b/"}, format="json")
        self.assertEqual(res.data["name"], "example.com/a/b")
        self.assertEqual(self.client.post(f"{self.base}/attachments/", {"kind": "link", "url": "javascript:alert(1)"}, format="json").status_code, 400)
        self.assertEqual(self.client.get(f"{self.base}/attachments/{res.data['id']}/download/").data["url"], "https://example.com/a/b/")

    @override_settings(ATTACHMENTS_DIR="")
    def test_upload_refused_without_bucket(self):
        upload = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")
        res = self.client.post(f"{self.base}/attachments/", {"kind": "file", "file": upload}, format="multipart")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(self.client.get("/api/config/").data["uploads"]["enabled"])

    def test_upload_to_local_disk_with_signed_link(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder, override_settings(ATTACHMENTS_DIR=folder):
            upload = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")
            res = self.client.post(f"{self.base}/attachments/", {"kind": "file", "file": upload}, format="multipart")
            self.assertEqual(res.status_code, 201, res.data)
            url = self.client.get(f"{self.base}/attachments/{res.data['id']}/download/").data["url"]
            self.assertIn("/api/files/", url)
            self.client.force_authenticate(None)
            got = self.client.get(url[url.index("/api/files/"):])
            self.assertEqual(b"".join(got.streaming_content), b"hello")
            self.assertEqual(self.client.get("/api/files/forged/").status_code, 404)

    @override_settings(STORAGES=WITH_BUCKET, ATTACHMENT_MAX_MB=1)
    def test_upload_download_delete(self):
        self.assertTrue(self.client.get("/api/config/").data["uploads"]["enabled"])
        upload = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")
        res = self.client.post(f"{self.base}/attachments/", {"kind": "file", "file": upload}, format="multipart")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual((res.data["name"], res.data["size"], res.data["content_type"]), ("notes.txt", 5, "text/plain"))
        att = Attachment.objects.get()
        self.assertTrue(att.file.name.startswith(f"attachments/{self.ws['id']}/{self.task['id']}/"))
        self.assertEqual(att.file.read(), b"hello")
        self.assertIn("notes.txt", self.client.get(f"{self.base}/attachments/{att.id}/download/").data["url"])

        big = SimpleUploadedFile("big.bin", b"x" * (1024 * 1024 + 1))
        self.assertEqual(self.client.post(f"{self.base}/attachments/", {"kind": "file", "file": big}, format="multipart").status_code, 400)

        self.as_user(self.bob)
        self.assertEqual(self.client.delete(f"{self.base}/attachments/{att.id}/").status_code, 403)
        self.as_user(self.alice)
        name = att.file.name
        storage = att.file.storage
        self.assertEqual(self.client.delete(f"{self.base}/attachments/{att.id}/").status_code, 204)
        self.assertFalse(storage.exists(name))

    def test_outsiders_cannot_read(self):
        carol = self.client.post("/api/auth/register/", {"email": "carol@example.com", "password": "s3cure-Passw0rd"}).data
        self.as_user(carol)
        self.assertEqual(self.client.get(f"{self.base}/comments/").status_code, 404)
