"""A line whose first part Ctrl+] sends to the background (issue found in
discussion #76): the rest must not run in the foreground meanwhile."""

from eosh.shell import Shell


def test_the_rest_of_a_parked_line_does_not_run(monkeypatch, capsys):
    sh = Shell()
    ran = []

    def fake_pipeline(pipeline, **kw):
        ran.append(pipeline.stages[0].text)
        if len(ran) == 1:
            sh._backgrounded = True       # what _park does on Ctrl+]
        return 0

    monkeypatch.setattr(sh, "_execute_pipeline", fake_pipeline)
    assert sh._execute("make && echo after; echo later") is None
    assert ran == ["make"]
    assert "the rest of the line was not run" in capsys.readouterr().err


def test_a_line_that_stays_in_front_runs_whole(monkeypatch):
    sh = Shell()
    ran = []
    monkeypatch.setattr(sh, "_execute_pipeline",
                        lambda p, **kw: ran.append(p.stages[0].text) or 0)
    assert sh._execute("make && echo after; echo later") == 0
    assert ran == ["make", "echo after", "echo later"]
