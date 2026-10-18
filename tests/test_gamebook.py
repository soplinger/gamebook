import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

import gamebook
from gamebook import StoryError, count_simple_paths, find_loops, parse, play, shortest_routes, to_dot, to_html

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "lighthouse.gb"


def errors_of(text):
    _, issues = parse(text)
    return [(i.line, i.message) for i in issues if i.severity == "error"]


def warnings_of(text):
    _, issues = parse(text)
    return [(i.line, i.message) for i in issues if i.severity == "warning"]


def test_example_parses_cleanly():
    story, issues = parse(EXAMPLE.read_text(encoding="utf-8"))
    assert issues == []
    assert story.title == "The Lighthouse at Gull Point"
    assert story.start == "gate"
    assert len(story.passages) == 11
    assert story.passages["lamp_room"].ending is True
    assert [c.target for c in story.passages["gate"].choices] == ["tower_door", "wreck", "village_end"]


def test_start_defaults_to_first_passage():
    story, issues = parse("@passage a\nHi.\n- Go -> b\n@passage b ending\nBye.\n")
    assert story.start == "a"
    assert issues == []


def test_unknown_directive_reports_line():
    assert errors_of("@passage a ending\nText.\n@colour red\n") == [(3, "unknown directive @colour")]


def test_choice_outside_passage_is_an_error():
    assert (1, "choice appears before any @passage") in errors_of("- Go -> a\n")


def test_choice_without_arrow_reports_its_line():
    text = "@passage a\nHi.\n- Go to the door\n@passage door ending\nIn.\n"
    assert (3, "choice needs the form '- label -> passage-id'") in errors_of(text)


def test_link_to_missing_passage_points_at_choice_line():
    text = "@passage a\nHi.\n- Go -> nowhere\n"
    assert (3, "choice links to missing passage 'nowhere'") in errors_of(text)


def test_duplicate_passage_id_names_the_first_definition():
    text = "@passage a ending\nOne.\n@passage a ending\nTwo.\n"
    assert (3, "passage 'a' is already defined on line 1") in errors_of(text)


def test_duplicate_passage_still_checks_its_links():
    text = "@passage a ending\nOne.\n@passage a\n- Go -> missing\n"
    messages = [m for _, m in errors_of(text)]
    assert "choice links to missing passage 'missing'" in messages


def test_ending_with_choices_is_an_error():
    text = "@passage a ending\nText.\n- Go -> b\n@passage b ending\nEnd.\n"
    assert (1, "ending 'a' has choices") in errors_of(text)


def test_dead_end_without_ending_mark_is_an_error():
    text = "@passage a\nText.\n"
    assert (1, "dead end 'a': add a choice or mark it 'ending'") in errors_of(text)


def test_unreachable_passage_is_a_warning():
    text = "@passage a ending\nText.\n@passage orphan ending\nLost.\n"
    assert (3, "passage 'orphan' is unreachable from start 'a'") in warnings_of(text)


def test_no_reachable_ending_is_a_warning():
    text = "@passage a\nLoop.\n- Again -> a\n"
    assert (None, "no ending is reachable from the start") in warnings_of(text)


def test_empty_passage_and_duplicate_choice_are_warnings():
    text = "@passage a\n- Go -> b\n- Go -> b\n@passage b ending\n"
    messages = warnings_of(text)
    assert (1, "passage 'a' has no text") in messages
    assert (3, "duplicate choice to 'b'") in messages


def test_shortest_routes_report_steps_and_words():
    story, _ = parse(EXAMPLE.read_text(encoding="utf-8"))
    routes = {r.ending: r for r in shortest_routes(story)}
    assert set(routes) == {"lamp_room", "storm_end", "village_end"}
    village = routes["village_end"]
    assert village.ids == ["gate", "village_end"]
    assert len(village.steps) == 1
    assert village.words == story.passages["gate"].words + story.passages["village_end"].words
    assert [target for _, target in routes["lamp_room"].steps][-1] == "lamp_room"


def test_simple_path_count_on_a_diamond():
    text = (
        "@passage s\nStart.\n- Left -> a\n- Right -> b\n"
        "@passage a\nA.\n- Down -> e\n"
        "@passage b\nB.\n- Down -> e\n"
        "@passage e ending\nEnd.\n"
    )
    story, _ = parse(text)
    assert count_simple_paths(story) == (2, False)


def test_simple_path_count_stops_at_limit():
    text = (
        "@passage s\nStart.\n- One -> a\n- Two -> b\n"
        "@passage a\nA.\n- Go -> e\n"
        "@passage b\nB.\n- Go -> e\n"
        "@passage e ending\nEnd.\n"
    )
    story, _ = parse(text)
    assert count_simple_paths(story, limit=1) == (1, True)


def test_loops_are_found_from_the_start():
    story, _ = parse(EXAMPLE.read_text(encoding="utf-8"))
    loops = find_loops(story)
    assert sorted(loops) == [["gate", "tower_door", "gate"], ["gate", "wreck", "gate"]]


def test_play_by_number_and_label_reaches_an_ending():
    story, _ = parse(EXAMPLE.read_text(encoding="utf-8"))
    steps, ended = play(story, ["1", "Read the tide table", "1", "1", "1", "1"])
    assert ended is True
    assert [s.id for s in steps][-1] == "lamp_room"
    assert steps[0].chosen_number == 1


def test_play_without_picks_waits_at_start():
    story, _ = parse(EXAMPLE.read_text(encoding="utf-8"))
    steps, ended = play(story, [])
    assert ended is False
    assert [s.id for s in steps] == ["gate"]


def test_play_rejects_bad_picks():
    story, _ = parse(EXAMPLE.read_text(encoding="utf-8"))
    with pytest.raises(StoryError, match="out of range"):
        play(story, ["9"])
    with pytest.raises(StoryError, match="is not a choice"):
        play(story, ["Fly away"])


def test_play_rejects_unused_picks_after_an_ending():
    story, _ = parse("@passage a ending\nEnd.\n")
    with pytest.raises(StoryError, match="unused pick"):
        play(story, ["1"])


def test_dot_export_has_nodes_edges_and_escaped_labels():
    text = '@passage a\nHi.\n- Say "hi" -> b\n@passage b ending\nBye.\n'
    story, _ = parse(text)
    dot = to_dot(story)
    assert dot.startswith("digraph gamebook {")
    assert '"a" -> "b" [label="Say \\"hi\\""];' in dot
    assert '"b" [shape=doublecircle];' in dot


class _Collector(HTMLParser):
    """Collects the title text and script body of a page; raises on malformed tags."""

    def __init__(self):
        super().__init__()
        self.titles = []
        self.script_text = ""
        self._in = None

    def handle_starttag(self, tag, attrs):
        self._in = tag

    def handle_endtag(self, tag):
        self._in = None

    def handle_data(self, data):
        if self._in == "title":
            self.titles.append(data)
        elif self._in == "script":
            self.script_text += data


def test_html_export_parses_and_embeds_safe_json():
    text = "@title Trap <b>\n@passage a\nSay </script> now & then.\n- Go -> b\n@passage b ending\nDone.\n"
    story, _ = parse(text)
    page = to_html(story)
    collector = _Collector()
    collector.feed(page)
    collector.close()
    assert collector.titles == ["Trap <b>"]
    assert "</script>" not in collector.script_text
    assert "\\u003c/script\\u003e" in collector.script_text
    payload = collector.script_text.split("const STORY = ", 1)[1].split(";\n", 1)[0]
    data = json.loads(payload)
    assert data["passages"]["a"]["text"] == ["Say </script> now & then."]
    assert data["passages"]["b"]["ending"] is True


def test_cli_lint_exit_codes(tmp_path, capsys):
    assert gamebook.main(["lint", str(EXAMPLE)]) == 0
    assert "ok: 11 passage(s), no issues" in capsys.readouterr().out

    broken = tmp_path / "broken.gb"
    broken.write_text("@passage a\nHi.\n- Go -> nowhere\n", encoding="utf-8")
    assert gamebook.main(["lint", str(broken)]) == 1
    assert f"{broken}:3: error: choice links to missing passage 'nowhere'" in capsys.readouterr().out

    warned = tmp_path / "warned.gb"
    warned.write_text("@passage a ending\nEnd.\n@passage lost ending\nGone.\n", encoding="utf-8")
    assert gamebook.main(["lint", str(warned)]) == 0
    assert gamebook.main(["lint", str(warned), "--strict"]) == 1


def test_cli_analyze_json_and_missing_file(tmp_path, capsys):
    assert gamebook.main(["analyze", str(EXAMPLE), "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["passages"] == 11
    assert info["endings"] == 3
    assert info["unreachable"] == []
    assert info["reading_minutes"] == round(info["words"] / 200, 1)

    assert gamebook.main(["lint", str(tmp_path / "absent.gb")]) == 2


def test_cli_play_and_html_export(tmp_path, capsys):
    assert gamebook.main(["play", str(EXAMPLE), "2", "2"]) == 0
    out = capsys.readouterr().out
    assert "== wreck ==" in out
    assert "> 2) Return to the gate" in out
    assert "-- Waiting for a choice" in out

    target = tmp_path / "lighthouse.html"
    assert gamebook.main(["html", str(EXAMPLE), "-o", str(target)]) == 0
    assert target.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
