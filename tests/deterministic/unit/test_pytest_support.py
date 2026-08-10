from pathlib import Path


def test_repository_tmp_path_has_a_bounded_unique_segment(tmp_path: Path) -> None:
    assert tmp_path.name.startswith("t-")
    assert len(tmp_path.name) == 14
