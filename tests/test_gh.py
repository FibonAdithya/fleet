from __future__ import annotations

import pytest

from fleet.gh import FakeGh
from fleet.labels import area_label, attempt_label, state_label


def test_create_issue_returns_increasing_numbers():
    gh = FakeGh()
    assert gh.create_issue("first", "body", ["fleet:triage"]) == 1
    assert gh.create_issue("second", "body", ["fleet:triage"]) == 2


def test_comment_is_recorded_in_order():
    gh = FakeGh()
    n = gh.create_issue("t", "b", [])
    gh.comment(n, "one")
    gh.comment(n, "two")
    assert gh.get_issue(n)["comments"] == ["one", "two"]


def test_labels_add_and_remove():
    gh = FakeGh()
    n = gh.create_issue("t", "b", [state_label("ready")])
    gh.add_labels(n, [area_label("eval"), attempt_label(1)])
    gh.remove_labels(n, [state_label("ready")])
    names = gh.get_issue(n)["labels"]
    assert "area:eval" in names and "fleet:attempt-1" in names
    assert "fleet:ready" not in names


def test_removing_an_absent_label_is_not_an_error():
    gh = FakeGh()
    n = gh.create_issue("t", "b", [])
    gh.remove_labels(n, ["fleet:claimed"])
    assert gh.get_issue(n)["labels"] == []


def test_list_issues_filters_by_state():
    gh = FakeGh()
    gh.create_issue("open one", "b", [])
    closed = gh.create_issue("closed one", "b", [])
    gh.close_issue(closed)
    assert [i["title"] for i in gh.list_issues("open")] == ["open one"]
    assert len(gh.list_issues("all")) == 2


def test_search_titles_is_case_insensitive_substring():
    gh = FakeGh()
    gh.create_issue("estimate_lid crashes", "b", [])
    assert len(gh.search_titles("CRASHES")) == 1
    assert gh.search_titles("nothing here") == []


def test_unknown_issue_raises():
    with pytest.raises(KeyError):
        FakeGh().get_issue(99)


def test_add_labels_does_not_duplicate():
    gh = FakeGh()
    n = gh.create_issue("t", "b", [area_label("eval")])
    gh.add_labels(n, [area_label("eval"), state_label("ready")])
    assert gh.get_issue(n)["labels"] == ["area:eval", "fleet:ready"]


def test_applying_an_unknown_label_fails_like_gh_does():
    """`gh issue create --label X` fails when X does not exist and creates no
    issue. A fake that accepts anything hides every missing ensure_label call."""
    gh = FakeGh()
    with pytest.raises(KeyError, match="discovered-by:1"):
        gh.create_issue("t", "b", ["discovered-by:1"])
    gh.ensure_label("discovered-by:1")
    assert gh.create_issue("t", "b", ["discovered-by:1"]) == 1


def test_list_issues_omits_comments_like_the_real_client():
    """RealGh's `issue list` does not fetch comment bodies. A fake that returns
    them lets code pass every test here and KeyError against real GitHub."""
    gh = FakeGh()
    n = gh.create_issue("t", "b", [])
    gh.comment(n, "hello")
    assert "comments" not in gh.list_issues("open")[0]
    assert gh.get_issue(n)["comments"] == ["hello"]
