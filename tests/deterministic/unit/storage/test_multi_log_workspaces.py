"""Per-package Logparse workspaces keep their existing Job cleanup owner."""
import pytest

from problem_locator.storage.paths import job_workspace_names, workspace_owner_id

JOB = "00000000-0000-0000-0000-000000000001"
ATTACHMENT = "00000000-0000-0000-0000-000000000002"


def test_cleanup_finds_only_valid_package_workspaces_for_this_job(tmp_path):
    package = JOB + ".logparse-preprocess." + ATTACHMENT
    invalid = JOB + ".logparse-preprocess.unknown"
    other = ATTACHMENT + ".logparse-preprocess." + JOB
    for name in (package, invalid, other):
        (tmp_path / name).mkdir()
    assert set(job_workspace_names(JOB, tmp_path)) == {
        JOB, JOB + ".logparse-preprocess", JOB + ".generic-preprocess", package}
    assert workspace_owner_id(package) == JOB
    assert workspace_owner_id(JOB + ".logparse-preprocess") == JOB
    with pytest.raises(ValueError):
        workspace_owner_id(invalid)


def test_direct_generic_preparation_has_the_same_cleanup_owner():
    assert workspace_owner_id(JOB + ".generic-preprocess") == JOB
    assert JOB + ".generic-preprocess" in job_workspace_names(JOB)
    for invalid in (JOB + ".generic-preprocess.extra",
                    JOB + ".generic-preprocess." + ATTACHMENT,
                    "unknown.generic-preprocess"):
        with pytest.raises(ValueError):
            workspace_owner_id(invalid)
