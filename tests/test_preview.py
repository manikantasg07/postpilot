import pytest

from agent import preview
from agent.preview import PreviewError, build_body, decide, extract_text, title_for


def body(text="Post body here."):
    return build_body("A topic", text, "09:00", "post", 30)


def test_body_round_trips_the_post():
    assert extract_text(body("Line one.\n\nLine two.")) == "Line one.\n\nLine two."


def test_edited_body_is_what_gets_extracted():
    edited = body("Original.").replace("Original.", "I fixed this sentence.")
    assert extract_text(edited) == "I fixed this sentence."


def test_missing_markers_is_an_error():
    with pytest.raises(PreviewError, match="markers"):
        extract_text("someone deleted the whole body")


def test_title_identifies_date_and_slot():
    assert title_for("2026-09-22", "09:00") == "Draft post for 2026-09-22 09:00"


@pytest.mark.parametrize("comments,expected", [
    ([], "none"),
    ([{"body": "nice one"}], "none"),
    ([{"body": "/approve"}], "approve"),
    ([{"body": "/cancel"}], "cancel"),
    ([{"body": "/approve"}, {"body": "/cancel changed my mind"}], "cancel"),
    ([{"body": "/cancel"}, {"body": "/approve after all"}], "approve"),
    ([{"body": "I would /approve this but not yet"}], "none"),
])
def test_decide_reads_the_newest_instruction(comments, expected):
    assert decide(comments) == expected


# --- the draft has to actually reach your phone -----------------------------

def test_the_draft_is_assigned_to_you(monkeypatch):
    """GitHub Mobile pushes for "Assigned", never for "an issue was opened in a
    repo you watch". A draft nobody is told about is not a review step."""
    sent = {}
    monkeypatch.setattr(preview, "_call",
                        lambda method, path, token, **kw: sent.update(kw.get("json", {}))
                        or {"number": 7})

    preview.create_draft("2026-09-29", "16:50", "Topic", "text", "post", 30,
                         token="gho_x", repo="sud018/linkedin-agent")

    assert sent["assignees"] == ["sud018"]
    assert sent["labels"] == [preview.LABEL]


def test_the_body_mentions_you_as_well(monkeypatch):
    """A second route, for anyone whose assignment pushes are switched off."""
    sent = {}
    monkeypatch.setattr(preview, "_call",
                        lambda method, path, token, **kw: sent.update(kw.get("json", {}))
                        or {"number": 7})

    preview.create_draft("2026-09-29", "16:50", "Topic", "text", "post", 30,
                         token="gho_x", repo="sud018/linkedin-agent")

    assert sent["body"].startswith("@sud018")


def test_the_mention_never_reaches_linkedin():
    """It sits above the markers, so the published text is untouched."""
    body = build_body("Topic", "the post itself", "16:50", "post", 30, mention="sud018")
    assert extract_text(body) == "the post itself"
    assert "@sud018" not in extract_text(body)


def test_a_draft_without_a_mention_still_works():
    """The parameter is optional, so nothing that calls it the old way breaks."""
    body = build_body("Topic", "the post itself", "16:50", "post", 30)
    assert not body.startswith("@")
    assert extract_text(body) == "the post itself"


@pytest.mark.parametrize("repo, owner", [
    ("sud018/linkedin-agent", "sud018"),
    ("some-org/some-repo", "some-org"),
])
def test_the_owner_comes_from_the_repository(repo, owner):
    assert preview.owner_of(repo) == owner


# --- reading and editing the issues from the app -----------------------------

def issue(number=7, title="Draft post for 2026-09-29 17:40", text="the post"):
    return {"number": number, "title": title,
            "body": build_body("Evaluating RAG", text, "17:40", "post", 30, mention="sud018"),
            "created_at": "2026-09-29T23:10:00Z",
            "html_url": f"https://github.com/me/agent/issues/{number}"}


def test_open_drafts_reads_what_the_page_needs(monkeypatch):
    monkeypatch.setattr(preview, "_call",
                        lambda method, path, token, **kw: [issue()] if "/issues?" in path else [])

    [found] = preview.open_drafts("gho_x", "me/agent")

    assert (found.number, found.date, found.slot) == (7, "2026-09-29", "17:40")
    assert found.topic == "Evaluating RAG"
    assert found.text == "the post"          # the mention and instructions are stripped
    assert found.decision == "none"


def test_a_comment_on_the_issue_is_read_as_the_decision(monkeypatch):
    monkeypatch.setattr(preview, "_call",
                        lambda method, path, token, **kw: [issue()] if "/issues?" in path
                        else [{"body": "/approve"}])

    assert preview.open_drafts("gho_x", "me/agent")[0].decision == "approve"


def test_an_issue_without_markers_is_not_ours(monkeypatch):
    """Somebody else's issue carrying the label must not be shown as a draft."""
    monkeypatch.setattr(preview, "_call",
                        lambda method, path, token, **kw:
                        [{"number": 9, "title": "Something else", "body": "no markers"}]
                        if "/issues?" in path else [])

    assert preview.open_drafts("gho_x", "me/agent") == []


def test_editing_replaces_only_the_post(monkeypatch):
    """The instructions and the @mention around it have to survive."""
    sent = {}
    monkeypatch.setattr(preview, "_call",
                        lambda method, path, token, **kw: sent.update(kw.get("json", {}))
                        or issue())

    preview.set_draft_text(7, "my own words", "gho_x", "me/agent")

    assert extract_text(sent["body"]) == "my own words"
    assert sent["body"].startswith("@sud018")
    assert "/approve" in sent["body"]        # the instructions are still there


def test_editing_refuses_a_body_it_does_not_recognise(monkeypatch):
    monkeypatch.setattr(preview, "_call",
                        lambda method, path, token, **kw: {"body": "somebody removed the markers"})

    with pytest.raises(PreviewError, match="markers"):
        preview.set_draft_text(7, "new", "gho_x", "me/agent")
