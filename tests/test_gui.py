from douyinfire.gui import JobState


def test_job_state_tracks_steps() -> None:
    job = JobState()

    assert job.start("run:main") is True
    job.update_step("open_home", "running", "main")
    job.update_step("open_home", "done", "main")
    snapshot = job.snapshot()

    assert snapshot["running"] is True
    assert snapshot["steps"][0]["key"] == "open_home"
    assert snapshot["steps"][0]["status"] == "done"


def test_job_state_marks_running_step_failed() -> None:
    job = JobState()

    job.start("run:main")
    job.update_step("contact_search", "running", "main")
    job.finish("failed", error="not found")
    contact_step = [step for step in job.snapshot()["steps"] if step["key"] == "contact_search"][0]

    assert contact_step["status"] == "failed"
    assert contact_step["error"] == "not found"
