import pytest
from fastapi import HTTPException

from orchestrator.api.docker.schema import SiteInfo
from orchestrator.api.files import router


def test_file_deletion_removes_site_directory(site_info: SiteInfo):
    directory = site_info.directory_path()
    (directory / "index.html").write_text("Site content")

    assert router.delete_all_site_files(site_info) == {}
    assert not directory.exists()


def test_file_deletion_reports_permission_failure(site_info: SiteInfo, monkeypatch):
    directory = site_info.directory_path()
    index = directory / "index.html"
    index.write_text("Site content")

    def denied_delete(_directory, **_kwargs):
        raise PermissionError("Permission denied")

    monkeypatch.setattr(router.shutil, "rmtree", denied_delete)
    with pytest.raises(HTTPException) as error:
        router.delete_all_site_files(site_info)
    assert error.value.status_code == 403
    assert index.read_text() == "Site content"


def test_file_deletion_tolerates_missing_directory(site_info: SiteInfo, monkeypatch):
    def already_deleted(_directory, **_kwargs):
        raise FileNotFoundError("Directory not found")

    monkeypatch.setattr(router.shutil, "rmtree", already_deleted)
    assert router.delete_all_site_files(site_info) == {}
