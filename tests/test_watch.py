"""Proactive watchers fire their notifier when the watched thing happens."""

import asyncio

from neovis.core.watch import WatchManager


async def _wait_for(results, tries=30):
    for _ in range(tries):
        if results:
            return
        await asyncio.sleep(0.1)


async def test_watch_command_notifies_on_completion():
    results = []

    async def notify(r):
        results.append(r)

    m = WatchManager(notify)
    m.watch_command("echo neovis-watch-ok", note="test job")
    await _wait_for(results)

    assert results, "notifier was not called"
    r = results[0]
    assert r.kind == "command" and r.ok and "neovis-watch-ok" in r.detail
    assert r.note == "test job"


async def test_watch_command_reports_failure():
    results = []

    async def notify(r):
        results.append(r)

    m = WatchManager(notify)
    m.watch_command("exit 3")
    await _wait_for(results)
    assert results and not results[0].ok and "exit code 3" in results[0].detail


async def test_watch_file_fires_only_when_it_appears(tmp_path):
    results = []

    async def notify(r):
        results.append(r)

    m = WatchManager(notify, poll=0.05)
    target = str(tmp_path / "done.flag")
    m.watch_file(target)
    await asyncio.sleep(0.2)
    assert not results  # not there yet

    (tmp_path / "done.flag").write_text("x")
    await _wait_for(results)
    assert results and results[0].kind == "file"
