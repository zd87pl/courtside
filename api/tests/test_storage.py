"""Key layout and multipart sizing. No network, no bucket."""

import uuid

import pytest

from courtside_api import storage

ACCOUNT = uuid.uuid4()
JOB = uuid.uuid4()


def test_video_keys_are_namespaced_per_account_and_job():
    key = storage.video_key(ACCOUNT, JOB, "match.mp4")
    assert key == f"uploads/{ACCOUNT}/{JOB}/source.mp4"
    # Two accounts can never collide, so one tenant's key can never name another's.
    assert storage.video_key(uuid.uuid4(), JOB, "match.mp4") != key


@pytest.mark.parametrize("filename,expected", [
    ("match.MOV", ".mov"),
    ("match.mp4", ".mp4"),
    ("no-extension", ".mp4"),
    ("../../etc/passwd", ".mp4"),          # traversal cannot escape the prefix
    ("x.thisisaverylongextension", ".mp4"),
    ("x.p4$", ".mp4"),                     # non-alphanumeric extension
])
def test_client_filenames_cannot_shape_the_key(filename, expected):
    key = storage.video_key(ACCOUNT, JOB, filename)
    assert key.endswith(f"source{expected}")
    assert key.startswith(f"uploads/{ACCOUNT}/{JOB}/")
    assert ".." not in key


def test_report_prefix_is_separate_from_the_upload_prefix():
    # Deleting the source after analysis must not touch the report.
    assert not storage.report_prefix(ACCOUNT, JOB).startswith("uploads/")


def test_part_plan_respects_the_s3_minimum():
    size, n = storage.part_plan(10 * 1024 * 1024)
    assert size >= storage.MIN_PART_BYTES and n >= 1


def test_a_typical_match_uploads_in_a_reasonable_number_of_parts():
    size, n = storage.part_plan(1_555_581_944)      # the 40-minute .mov in the repo
    assert n == -(-1_555_581_944 // size)
    assert 1 < n <= 64                              # resumable, not thousands of round trips


def test_part_size_grows_so_huge_files_stay_under_the_part_ceiling():
    size, n = storage.part_plan(600 * 1024**3)
    assert n <= storage.MAX_PARTS
    assert size > storage.DEFAULT_PART_BYTES


def test_a_single_byte_still_gets_one_part():
    assert storage.part_plan(1)[1] == 1
