"""Скачивание документов медкарты: сохранение, права и выбор варианта."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emias.errors import NotFound
from emias_mcp.errors import McpError
from emias_mcp.service import EmiasService, _guess_extension

PDF = b"%PDF-1.4\n%test\n"


class FakeClient:
    def __init__(self) -> None:
        self.pdf_calls = 0
        self.official_calls = 0
        self.archive_calls = 0
        self.scan_calls = 0
        self.info = {"officialDocumentPDFId": "official-1"}
        self.pdf_error: Exception | None = None

    def document_pdf(self, ehr_id, document_id):
        self.pdf_calls += 1
        if self.pdf_error:
            raise self.pdf_error
        return PDF

    def document_scan(self, ehr_id, document_id):
        self.scan_calls += 1
        return PDF

    def document_official_pdf(self, ehr_id, official_id):
        self.official_calls += 1
        return PDF

    def document_archive(self, ehr_id, official_id):
        self.archive_calls += 1
        return PDF

    def document_info(self, ehr_id, document_id):
        return self.info


class DocumentDownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["EMIAS_MCP_DISABLE_KEYCHAIN"] = "1"
        os.environ["EMIAS_MCP_IMPORT_CLI"] = "0"
        self.service = EmiasService(self.tmp.name)
        self.service.tokens.save({"access_token": "a"})
        self.client = FakeClient()

    def tearDown(self):
        os.environ.pop("EMIAS_MCP_DISABLE_KEYCHAIN", None)
        os.environ.pop("EMIAS_MCP_IMPORT_CLI", None)
        self.tmp.cleanup()

    def _download(self, document_id="doc-1", **kwargs):
        with mock.patch.object(
            self.service, "_patient_client", return_value=("ehr-1", self.client)
        ):
            return self.service.document_download(document_id, **kwargs)

    def test_saves_pdf_with_0600(self):
        result = self._download()
        path = Path(result["path"])
        self.assertTrue(path.exists())
        self.assertEqual(result["format"], "pdf")
        self.assertEqual(result["size"], len(PDF))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.read_bytes(), PDF)
        self.assertEqual(self.client.pdf_calls, 1)

    def test_rejects_bad_document_id(self):
        with self.assertRaises(McpError) as ctx:
            self._download(document_id="../../etc/passwd")
        self.assertEqual(ctx.exception.code, "bad_argument")

    def test_not_found(self):
        self.client.pdf_error = NotFound("нет файла")
        with self.assertRaises(McpError) as ctx:
            self._download()
        self.assertEqual(ctx.exception.code, "document_not_found")

    def test_official_pdf_uses_info(self):
        result = self._download(variant="official")
        self.assertEqual(result["variant"], "official")
        self.assertEqual(self.client.official_calls, 1)
        self.assertEqual(self.client.pdf_calls, 0)

    def test_official_pdf_without_id(self):
        self.client.info = {}
        with self.assertRaises(McpError) as ctx:
            self._download(variant="official")
        self.assertEqual(ctx.exception.code, "no_official_pdf")

    def test_scan_variant(self):
        result = self._download(variant="scan")
        self.assertEqual(result["variant"], "scan")
        self.assertEqual(self.client.scan_calls, 1)

    def test_bad_variant(self):
        with self.assertRaises(McpError) as ctx:
            self._download(variant="zip")
        self.assertEqual(ctx.exception.code, "bad_argument")

    def test_extension_detection(self):
        self.assertEqual(_guess_extension(b"%PDF-1.7"), "pdf")
        self.assertEqual(_guess_extension(b"\x89PNG\r\n"), "png")
        self.assertEqual(_guess_extension(b"\xff\xd8\xff\xe0"), "jpg")
        self.assertEqual(_guess_extension(b"PK\x03\x04"), "bin")


if __name__ == "__main__":
    unittest.main()
