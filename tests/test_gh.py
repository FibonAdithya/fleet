from __future__ import annotations

import pytest

from fleet.gh import FakeGh, GhClient, GhError, RealGh
from fleet.labels import area_label, attempt_label, state_label, static_vocabulary


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
    with pytest.raises(GhError):
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
    with pytest.raises(GhError, match="discovered-by:1"):
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


def test_stored_state_matches_what_real_gh_returns():
    """Real gh's JSON reports "OPEN"/"CLOSED", not "open"/"closed" -- proven by
    fixtures/ledger/issues.json. A fake storing lowercase passes every test
    here and returns nothing against a predicate written for the real casing."""
    gh = FakeGh()
    n = gh.create_issue("t", "b", [])
    assert gh.get_issue(n)["state"] == "OPEN"
    gh.close_issue(n)
    assert gh.get_issue(n)["state"] == "CLOSED"


def test_list_issues_state_filter_is_case_insensitive():
    """Real gh's --state flag treats "open" and "OPEN" identically. list_issues
    must too, regardless of which casing a caller passes."""
    gh = FakeGh()
    n = gh.create_issue("open one", "b", [])
    closed = gh.create_issue("closed one", "b", [])
    gh.close_issue(closed)
    assert [i["number"] for i in gh.list_issues("open")] == [n]
    assert [i["number"] for i in gh.list_issues("OPEN")] == [n]
    assert [i["number"] for i in gh.list_issues("Open")] == [n]
    assert len(gh.list_issues("all")) == 2
    assert len(gh.list_issues("ALL")) == 2


def test_add_labels_with_empty_list_is_a_noop():
    """Not reachable through today's call sites, but the M2 allocator will
    compute label lists that can come out empty. RealGh already guards this in
    create_issue; add_labels/remove_labels need the same guard, and FakeGh must
    agree with it."""
    gh = FakeGh()
    n = gh.create_issue("t", "b", [])
    gh.add_labels(n, [])
    assert gh.get_issue(n)["labels"] == []


def test_remove_labels_with_empty_list_is_a_noop():
    gh = FakeGh()
    n = gh.create_issue("t", "b", [area_label("eval")])
    gh.remove_labels(n, [])
    assert gh.get_issue(n)["labels"] == ["area:eval"]


def test_add_or_remove_labels_with_empty_list_does_not_require_the_issue_to_exist():
    """RealGh never calls gh at all when the label list is empty, so it never
    finds out whether the issue exists either. FakeGh must be a true no-op the
    same way, not a validated one."""
    gh = FakeGh()
    gh.add_labels(999, [])
    gh.remove_labels(999, [])


def test_get_issue_returns_a_copy_not_the_live_record():
    """RealGh parses a fresh dict from gh's JSON on every call; mutating it does
    nothing to GitHub. FakeGh handing back its own stored dict would let that
    same mutation silently corrupt the fake's ledger, passing every test here
    while writing nothing live."""
    gh = FakeGh()
    n = gh.create_issue("t", "b", [area_label("eval")])
    gh.comment(n, "hello")
    snapshot = gh.get_issue(n)
    snapshot["labels"].append("area:data")
    snapshot["comments"].append("bye")
    snapshot["title"] = "mutated"
    fresh = gh.get_issue(n)
    assert fresh["labels"] == ["area:eval"]
    assert fresh["comments"] == ["hello"]
    assert fresh["title"] == "t"


def test_list_issues_returns_copies_not_the_live_records():
    """The same defect get_issue was fixed for. A dict comprehension is a
    shallow copy, so the rows shared their `labels` list with the store: an
    allocator that builds a label list in place would corrupt the ledger,
    pass every test here, and do nothing against GitHub."""
    gh = FakeGh()
    n = gh.create_issue("t", "b", [area_label("eval")])
    row = gh.list_issues("open")[0]
    row["labels"].append("area:data")
    row["title"] = "mutated"
    assert gh.get_issue(n)["labels"] == ["area:eval"]
    assert gh.get_issue(n)["title"] == "t"


def test_search_titles_returns_copies_too():
    """search_titles builds on list_issues, so it inherited the same aliasing."""
    gh = FakeGh()
    n = gh.create_issue("estimate_lid crashes", "b", [area_label("eval")])
    gh.search_titles("crashes")[0]["labels"].append("area:data")
    assert gh.get_issue(n)["labels"] == ["area:eval"]


def test_removing_a_label_the_repo_never_had_fails_like_gh_does():
    """`gh issue edit --remove-label X` resolves X to an id first, so an X the
    repo has never had fails the whole edit. Only create_issue and add_labels
    mirrored that -- and _post's every transition goes through remove_labels,
    so a repo missing one state label passed the whole offline suite and failed
    on the first live write."""
    gh = FakeGh()
    n = gh.create_issue("t", "b", [area_label("eval")])
    with pytest.raises(GhError, match="discovered-by:1"):
        gh.remove_labels(n, ["discovered-by:1"])
    assert gh.get_issue(n)["labels"] == ["area:eval"]


def test_close_issue_is_on_the_protocol_not_only_the_fake():
    """A fake able to perform a transition the real client cannot is the exact
    divergence class that did all the damage in M1 -- it passes the whole
    offline suite and fails against GitHub."""
    assert hasattr(GhClient, "close_issue")
    assert hasattr(RealGh, "close_issue")
    assert hasattr(FakeGh, "close_issue")


def test_assign_is_on_the_protocol_not_only_the_fake():
    assert hasattr(GhClient, "assign")
    assert hasattr(RealGh, "assign")
    assert hasattr(FakeGh, "assign")


def test_assign_sets_and_clears_the_owner():
    gh = FakeGh()
    number = gh.create_issue("t", "b", ["fleet:ready"])
    gh.assign(number, "FibonAdithya")
    assert gh.get_issue(number)["assignees"] == ["FibonAdithya"]
    gh.assign(number, None)
    assert gh.get_issue(number)["assignees"] == []


def test_a_closed_issue_reports_closed():
    gh = FakeGh()
    number = gh.create_issue("t", "b", ["fleet:ready"])
    gh.close_issue(number)
    assert gh.get_issue(number)["state"] == "CLOSED"
    assert gh.list_issues("open") == []
    assert len(gh.list_issues("all")) == 1


def test_assign_rejects_a_login_gh_does_not_recognise():
    """Real `gh issue edit --add-assignee X` resolves X first and fails the
    whole edit when it cannot -- exactly the M1 divergence class (docs/m2-
    inputs.md §6): a fake that accepts any string here would pass this whole
    offline suite and then fail the first live write against GitHub."""
    gh = FakeGh()
    number = gh.create_issue("t", "b", ["fleet:ready"])
    with pytest.raises(GhError, match="not-a-real-user"):
        gh.assign(number, "not-a-real-user")
    assert gh.get_issue(number)["assignees"] == []


def test_assign_accepts_a_known_login():
    gh = FakeGh(known_logins={"octocat"})
    number = gh.create_issue("t", "b", ["fleet:ready"])
    gh.assign(number, "octocat")
    assert gh.get_issue(number)["assignees"] == ["octocat"]


def test_get_issue_returns_a_copy_of_assignees_not_the_live_list():
    """The same aliasing defect `get_issue`'s own docstring warns about for
    `labels` and `comments`: a shallow dict copy still shares the `assignees`
    list, so a caller mutating what it gets back would silently corrupt the
    fake's ledger while doing nothing live."""
    gh = FakeGh()
    n = gh.create_issue("t", "b", [])
    gh.assign(n, "FibonAdithya")
    snapshot = gh.get_issue(n)
    snapshot["assignees"].append("someone-else")
    assert gh.get_issue(n)["assignees"] == ["FibonAdithya"]


def test_list_issues_returns_a_copy_of_assignees_not_the_live_list():
    """`list_issues` had the same aliasing bug fixed for `labels` -- `assignees`
    needs the identical fix, or a caller that mutates a listed row's assignees
    corrupts the fake's ledger and passes every test here while doing nothing
    against GitHub."""
    gh = FakeGh()
    n = gh.create_issue("t", "b", [])
    gh.assign(n, "FibonAdithya")
    row = gh.list_issues("open")[0]
    row["assignees"].append("someone-else")
    assert gh.get_issue(n)["assignees"] == ["FibonAdithya"]


def test_the_label_vocabulary_tracks_the_configured_attempt_cap():
    """`max_attempts` is a fleet.toml knob, but the vocabulary hardcoded
    attempt-1 and attempt-2. Raising the cap produced a well-formed
    attempt_label(3) that no repo and no fake had ever heard of."""
    assert attempt_label(3) not in static_vocabulary()
    assert attempt_label(3) in static_vocabulary(max_attempts=3)
    with pytest.raises(GhError, match="attempt-3"):
        FakeGh().create_issue("t", "b", [attempt_label(3)])
    assert FakeGh(max_attempts=3).create_issue("t", "b", [attempt_label(3)]) == 1
