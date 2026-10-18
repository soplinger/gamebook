"""Gamebook: lint, analyse, play and export branching interactive-fiction files.

A gamebook is one plain-text file of passages and choices:

    @title The Lighthouse
    @start gate

    @passage gate
    A gate creaks open onto a wet headland.
    - Take the cliff path -> tower
    - Stay in the village -> village_end

    @passage village_end ending
    You never find out what the light was for.

Lines starting with "#" are comments. Any other plain line is prose for the
current passage, and "- label -> target" is a choice.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
DEFAULT_WPM = 200
DEFAULT_LIMIT = 10000


class StoryError(Exception):
    """Bad input to a playthrough."""


@dataclass
class Choice:
    label: str
    target: str
    line: int


@dataclass
class Passage:
    id: str
    line: int
    ending: bool = False
    text: list[str] = field(default_factory=list)
    choices: list[Choice] = field(default_factory=list)

    @property
    def words(self) -> int:
        return sum(len(paragraph.split()) for paragraph in self.text)


@dataclass
class Issue:
    severity: str  # "error" or "warning"
    line: int | None
    message: str

    def format(self, path: str) -> str:
        where = f"{path}:{self.line}" if self.line is not None else path
        return f"{where}: {self.severity}: {self.message}"


@dataclass
class Story:
    title: str = ""
    start: str | None = None
    start_line: int | None = None
    passages: dict[str, Passage] = field(default_factory=dict)


@dataclass
class Route:
    ending: str
    ids: list[str]
    steps: list[tuple[str, str]]  # (choice label, target id)
    words: int


@dataclass
class Step:
    id: str
    text: list[str]
    choices: list[Choice]
    chosen_number: int | None = None


# --- parsing and lint -------------------------------------------------------


def parse(text: str) -> tuple[Story, list[Issue]]:
    """Parse a gamebook file. Returns the story and every lint issue found."""
    story = Story()
    issues: list[Issue] = []
    every: list[Passage] = []  # includes rejected duplicates so their links still get checked
    current: Passage | None = None

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("@"):
            name, _, rest = line[1:].partition(" ")
            rest = rest.strip()
            if name == "title":
                if rest:
                    story.title = rest
                else:
                    issues.append(Issue("error", number, "@title needs a title"))
            elif name == "start":
                if not ID_RE.match(rest):
                    issues.append(Issue("error", number, "@start needs a valid passage id"))
                elif story.start is not None:
                    issues.append(Issue("error", number, f"start already set on line {story.start_line}"))
                else:
                    story.start, story.start_line = rest, number
            elif name == "passage":
                current = _open_passage(story, issues, number, rest)
                every.append(current)
            else:
                issues.append(Issue("error", number, f"unknown directive @{name}"))
            continue

        if line.startswith("- "):
            if current is None:
                issues.append(Issue("error", number, "choice appears before any @passage"))
                continue
            label, arrow, target = line[2:].rpartition("->")
            label, target = label.strip(), target.strip()
            if not arrow:
                issues.append(Issue("error", number, "choice needs the form '- label -> passage-id'"))
            elif not label:
                issues.append(Issue("error", number, "choice has no label"))
            elif not ID_RE.match(target):
                issues.append(Issue("error", number, f"invalid target id {target!r}"))
            else:
                current.choices.append(Choice(label, target, number))
            continue

        if current is None:
            issues.append(Issue("error", number, "text appears before any @passage"))
        else:
            current.text.append(line)

    if not story.passages:
        issues.append(Issue("error", None, "no @passage defined"))
        return story, _sorted(issues)

    if story.start is None:
        story.start = next(iter(story.passages))
    elif story.start not in story.passages:
        issues.append(Issue("error", story.start_line, f"start passage {story.start!r} is not defined"))
        return story, _sorted(issues)

    for passage in every:
        for choice in passage.choices:
            if choice.target not in story.passages:
                issues.append(Issue("error", choice.line, f"choice links to missing passage {choice.target!r}"))
        seen: set[tuple[str, str]] = set()
        for choice in passage.choices:
            key = (choice.label, choice.target)
            if key in seen:
                issues.append(Issue("warning", choice.line, f"duplicate choice to {choice.target!r}"))
            seen.add(key)

        if story.passages.get(passage.id) is not passage:
            continue  # rejected duplicate: only its links were checked above
        if passage.ending and passage.choices:
            issues.append(Issue("error", passage.line, f"ending {passage.id!r} has choices"))
        elif not passage.ending and not passage.choices:
            issues.append(Issue("error", passage.line, f"dead end {passage.id!r}: add a choice or mark it 'ending'"))
        if not passage.text:
            issues.append(Issue("warning", passage.line, f"passage {passage.id!r} has no text"))

    parent, _ = _search(story)
    for pid, passage in story.passages.items():
        if pid not in parent:
            issues.append(Issue("warning", passage.line, f"passage {pid!r} is unreachable from start {story.start!r}"))
    if not any(story.passages[pid].ending for pid in parent):
        issues.append(Issue("warning", None, "no ending is reachable from the start"))

    return story, _sorted(issues)


def _open_passage(story: Story, issues: list[Issue], number: int, rest: str) -> Passage:
    tokens = rest.split()
    pid = tokens[0] if tokens else ""
    passage = Passage(id=pid or "?", line=number)
    if not pid:
        issues.append(Issue("error", number, "@passage needs an id"))
    elif not ID_RE.match(pid):
        issues.append(Issue("error", number, f"invalid passage id {pid!r}"))
    elif pid in story.passages:
        first = story.passages[pid].line
        issues.append(Issue("error", number, f"passage {pid!r} is already defined on line {first}"))
    else:
        story.passages[pid] = passage

    for attribute in tokens[1:]:
        if attribute == "ending":
            passage.ending = True
        else:
            issues.append(Issue("error", number, f"unknown passage attribute {attribute!r}"))
    return passage


def _sorted(issues: list[Issue]) -> list[Issue]:
    return sorted(issues, key=lambda issue: (issue.line is None, issue.line or 0))


# --- graph analysis ---------------------------------------------------------


def _targets(story: Story, pid: str) -> list[str]:
    """Distinct, existing targets of a passage in choice order."""
    out: list[str] = []
    for choice in story.passages[pid].choices:
        if choice.target in story.passages and choice.target not in out:
            out.append(choice.target)
    return out


def _search(story: Story) -> tuple[dict[str, tuple[str, Choice] | None], list[str]]:
    """Breadth-first search from the start. Returns parent links and visit order."""
    parent: dict[str, tuple[str, Choice] | None] = {story.start: None}
    order = [story.start]
    queue = deque([story.start])
    while queue:
        pid = queue.popleft()
        for choice in story.passages[pid].choices:
            if choice.target in story.passages and choice.target not in parent:
                parent[choice.target] = (pid, choice)
                order.append(choice.target)
                queue.append(choice.target)
    return parent, order


def shortest_routes(story: Story) -> list[Route]:
    """Shortest route from the start to every reachable ending, in file order."""
    parent, _ = _search(story)
    routes = []
    for pid, passage in story.passages.items():
        if pid not in parent or not passage.ending:
            continue
        ids, steps = [pid], []
        node = pid
        while parent[node] is not None:
            prev, choice = parent[node]
            steps.append((choice.label, node))
            ids.append(prev)
            node = prev
        ids.reverse()
        steps.reverse()
        words = sum(story.passages[i].words for i in ids)
        routes.append(Route(pid, ids, steps, words))
    return routes


def count_simple_paths(story: Story, limit: int = DEFAULT_LIMIT) -> tuple[int, bool]:
    """Count routes from start to an ending that never revisit a passage.

    Stops at ``limit``. Returns (count, truncated).
    """
    count = 0
    on_path: set[str] = set()

    def walk(pid: str) -> None:
        nonlocal count
        if count >= limit:
            return
        if story.passages[pid].ending:
            count += 1
            return
        on_path.add(pid)
        for target in _targets(story, pid):
            if target not in on_path:
                walk(target)
            if count >= limit:
                break
        on_path.discard(pid)

    walk(story.start)
    return count, count >= limit


def find_loops(story: Story) -> list[list[str]]:
    """Each back-edge reachable from the start, as the loop it closes."""
    loops: list[list[str]] = []
    state: dict[str, int] = {story.start: 1}  # 1 = on the current path, 2 = finished
    path = [story.start]
    stack = [(story.start, iter(_targets(story, story.start)))]
    while stack:
        pid, targets = stack[-1]
        nxt = next(targets, None)
        if nxt is None:
            stack.pop()
            path.pop()
            state[pid] = 2
        elif state.get(nxt) == 1:
            loops.append(path[path.index(nxt):] + [nxt])
        elif nxt not in state:
            state[nxt] = 1
            path.append(nxt)
            stack.append((nxt, iter(_targets(story, nxt))))
    return loops


def reading_minutes(words: int, wpm: int) -> float:
    return round(words / wpm, 1)


def summarise(story: Story, wpm: int = DEFAULT_WPM) -> dict:
    parent, _ = _search(story)
    words = sum(p.words for p in story.passages.values())
    return {
        "title": story.title,
        "start": story.start,
        "passages": len(story.passages),
        "endings": sum(1 for p in story.passages.values() if p.ending),
        "choices": sum(len(p.choices) for p in story.passages.values()),
        "words": words,
        "reading_minutes": reading_minutes(words, wpm),
        "reachable": len(parent),
        "unreachable": [pid for pid in story.passages if pid not in parent],
        "loops": find_loops(story),
    }


# --- playthroughs -----------------------------------------------------------


def _resolve(choices: list[Choice], pick: str, pid: str) -> tuple[int, Choice]:
    if pick.isdigit():
        number = int(pick)
        if 1 <= number <= len(choices):
            return number, choices[number - 1]
        raise StoryError(f"{pid!r} has {len(choices)} choice(s); {pick} is out of range")
    matches = [(n, c) for n, c in enumerate(choices, start=1) if c.label.casefold() == pick.casefold()]
    if not matches:
        raise StoryError(f"{pick!r} is not a choice at {pid!r}")
    if len(matches) > 1:
        raise StoryError(f"{pick!r} matches {len(matches)} choices at {pid!r}; use its number")
    return matches[0]


def play(story: Story, picks: list[str]) -> tuple[list[Step], bool]:
    """Follow the story using scripted picks (numbers or labels).

    Returns the visited steps and whether the story reached an ending.
    """
    steps: list[Step] = []
    remaining = list(picks)
    pid = story.start
    while True:
        passage = story.passages[pid]
        if passage.ending or not passage.choices:
            steps.append(Step(pid, passage.text, passage.choices))
            if remaining:
                raise StoryError(f"the story ended at {pid!r} with {len(remaining)} unused pick(s)")
            return steps, True
        if not remaining:
            steps.append(Step(pid, passage.text, passage.choices))
            return steps, False
        number, choice = _resolve(passage.choices, remaining.pop(0), pid)
        steps.append(Step(pid, passage.text, passage.choices, number))
        pid = choice.target


# --- exports ----------------------------------------------------------------


def _dot_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_dot(story: Story) -> str:
    lines = [
        "digraph gamebook {",
        "  rankdir=LR;",
        '  node [shape=box, style=rounded, fontname="Helvetica"];',
    ]
    for pid, passage in story.passages.items():
        attrs = []
        if passage.ending:
            attrs.append("shape=doublecircle")
        if pid == story.start:
            attrs.append("penwidth=2")
        suffix = f" [{', '.join(attrs)}]" if attrs else ""
        lines.append(f"  {_dot_quote(pid)}{suffix};")
    for pid, passage in story.passages.items():
        for choice in passage.choices:
            if choice.target in story.passages:
                lines.append(f"  {_dot_quote(pid)} -> {_dot_quote(choice.target)} [label={_dot_quote(choice.label)}];")
    lines.append("}")
    return "\n".join(lines) + "\n"


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="gamebook">
<title>__TITLE__</title>
<style>
  body { font-family: Georgia, "Times New Roman", serif; max-width: 40rem; margin: 2rem auto; padding: 0 1rem; color: #1d1d1d; background: #fbf8f1; line-height: 1.6; }
  h1 { font-size: 1.6rem; border-bottom: 1px solid #d8d2c4; padding-bottom: .4rem; }
  #scene p { margin: .8rem 0; }
  .ending { font-style: italic; color: #6b5a3a; }
  #choices { display: flex; flex-direction: column; gap: .5rem; margin: 1.2rem 0; }
  button { font: inherit; padding: .5rem .8rem; cursor: pointer; text-align: left; background: #fff; border: 1px solid #b8ad96; border-radius: 4px; }
  button:hover:not(:disabled) { background: #f1ebdc; }
  button:disabled { opacity: .5; cursor: default; }
  #controls { display: flex; gap: .5rem; }
</style>
</head>
<body>
<h1>__TITLE__</h1>
<main id="scene"></main>
<nav id="choices" aria-label="Choices"></nav>
<div id="controls">
  <button type="button" id="back">Back</button>
  <button type="button" id="restart">Restart</button>
</div>
<script>
"use strict";
const STORY = __DATA__;
const scene = document.getElementById("scene");
const choiceList = document.getElementById("choices");
const backButton = document.getElementById("back");
const trail = [];
let current = STORY.start;

function show(id) {
  const passage = STORY.passages[id];
  scene.replaceChildren();
  for (const paragraph of passage.text) {
    const p = document.createElement("p");
    p.textContent = paragraph;
    scene.appendChild(p);
  }
  choiceList.replaceChildren();
  if (passage.ending) {
    const end = document.createElement("p");
    end.className = "ending";
    end.textContent = "The End";
    scene.appendChild(end);
  } else {
    for (const choice of passage.choices) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = choice.label;
      button.addEventListener("click", () => {
        trail.push(current);
        current = choice.target;
        show(current);
      });
      choiceList.appendChild(button);
    }
  }
  backButton.disabled = trail.length === 0;
}

backButton.addEventListener("click", () => {
  if (trail.length) {
    current = trail.pop();
    show(current);
  }
});
document.getElementById("restart").addEventListener("click", () => {
  trail.length = 0;
  current = STORY.start;
  show(current);
});
show(current);
</script>
</body>
</html>
"""


def to_html(story: Story) -> str:
    """A self-contained page that plays the story in a browser. No network use."""
    data = {
        "start": story.start,
        "passages": {
            pid: {
                "text": passage.text,
                "ending": passage.ending or not passage.choices,
                "choices": [
                    {"label": c.label, "target": c.target}
                    for c in passage.choices
                    if c.target in story.passages
                ],
            }
            for pid, passage in story.passages.items()
        },
    }
    payload = (
        json.dumps(data, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    title = html.escape(story.title or "Gamebook")
    return HTML_TEMPLATE.replace("__TITLE__", title).replace("__DATA__", payload)


# --- command line -----------------------------------------------------------


def _refuse_on_errors(path: str, issues: list[Issue]) -> bool:
    """Print the issues to stderr. Returns True when the story has errors."""
    for issue in issues:
        print(issue.format(path), file=sys.stderr)
    errors = sum(1 for issue in issues if issue.severity == "error")
    if errors:
        print(f"gamebook: {errors} error(s) in {path}; run 'gamebook lint {path}' for the list", file=sys.stderr)
    return errors > 0


def _emit(text: str, output: str | None) -> int:
    if output is None:
        print(text, end="")
    else:
        Path(output).write_text(text, encoding="utf-8")
        print(f"wrote {output}")
    return 0


def cmd_lint(args, story: Story, issues: list[Issue]) -> int:
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    if args.json:
        print(json.dumps([asdict(i) for i in issues], indent=2))
    elif issues:
        for issue in issues:
            print(issue.format(args.file))
        print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    else:
        print(f"ok: {len(story.passages)} passage(s), no issues")
    return 1 if errors or (args.strict and warnings) else 0


def cmd_analyze(args, story: Story, issues: list[Issue]) -> int:
    if _refuse_on_errors(args.file, issues):
        return 1
    info = summarise(story, args.wpm)
    if args.json:
        print(json.dumps(info, indent=2))
        return 0
    print(info["title"] or args.file)
    print(f"Passages: {info['passages']} ({info['endings']} endings), choices: {info['choices']}")
    print(f"Words: {info['words']}, reading time {info['reading_minutes']} min at {args.wpm} wpm")
    print(f"Reachable from {info['start']!r}: {info['reachable']} of {info['passages']}")
    print("Unreachable: " + (", ".join(info["unreachable"]) or "none"))
    print(f"Loops: {len(info['loops'])}")
    for loop in info["loops"]:
        print("  " + " -> ".join(loop))
    return 0


def cmd_paths(args, story: Story, issues: list[Issue]) -> int:
    if _refuse_on_errors(args.file, issues):
        return 1
    routes = shortest_routes(story)
    if not routes:
        print("No ending can be reached from the start.")
        return 1
    print(f"Shortest route to each ending (from {story.start!r}):")
    for route in routes:
        minutes = reading_minutes(route.words, args.wpm)
        print(f"  {route.ending}: {len(route.steps)} step(s), {route.words} word(s), {minutes} min")
        path = route.ids[0] + "".join(f" --[{label}]--> {target}" for label, target in route.steps)
        print(f"    {path}")
    count, truncated = count_simple_paths(story, args.limit)
    prefix = "at least " if truncated else ""
    print(f"Simple routes from start to an ending: {prefix}{count}")
    return 0


def cmd_play(args, story: Story, issues: list[Issue]) -> int:
    if _refuse_on_errors(args.file, issues):
        return 1
    try:
        steps, ended = play(story, args.choices)
    except StoryError as exc:
        print(f"gamebook: {exc}", file=sys.stderr)
        return 1
    for step in steps:
        print(f"== {step.id} ==")
        for paragraph in step.text:
            print(paragraph)
        for number, choice in enumerate(step.choices, start=1):
            print(f"  {number}) {choice.label}")
        if step.chosen_number is not None:
            print(f"> {step.chosen_number}) {step.choices[step.chosen_number - 1].label}")
        print()
    if ended:
        print("-- The End --")
    else:
        print("-- Waiting for a choice: add more numbers or labels after the file name --")
    return 0


def cmd_dot(args, story: Story, issues: list[Issue]) -> int:
    if _refuse_on_errors(args.file, issues):
        return 1
    return _emit(to_dot(story), args.output)


def cmd_html(args, story: Story, issues: list[Issue]) -> int:
    if _refuse_on_errors(args.file, issues):
        return 1
    return _emit(to_html(story), args.output)


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}")
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gamebook",
        description="Lint, analyse, play and export branching interactive-fiction files.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    lint = sub.add_parser("lint", help="check the file with line-numbered errors and warnings")
    lint.add_argument("file")
    lint.add_argument("--strict", action="store_true", help="treat warnings as failures")
    lint.add_argument("--json", action="store_true", help="print issues as JSON")

    analyze = sub.add_parser("analyze", help="stats, reachability and loops")
    analyze.add_argument("file")
    analyze.add_argument("--wpm", type=_positive_int, default=DEFAULT_WPM, help="reading speed in words per minute")
    analyze.add_argument("--json", action="store_true", help="print the summary as JSON")

    paths = sub.add_parser("paths", help="shortest route to each ending and simple-route count")
    paths.add_argument("file")
    paths.add_argument("--wpm", type=_positive_int, default=DEFAULT_WPM)
    paths.add_argument("--limit", type=_positive_int, default=DEFAULT_LIMIT, help="stop counting routes at this number")

    play_cmd = sub.add_parser("play", help="walk the story with scripted choices")
    play_cmd.add_argument("file")
    play_cmd.add_argument("choices", nargs="*", help="choice numbers or labels, in order")

    dot = sub.add_parser("dot", help="export a Graphviz DOT graph")
    dot.add_argument("file")
    dot.add_argument("-o", "--output")

    export_html = sub.add_parser("html", help="export a self-contained playable HTML page")
    export_html.add_argument("file")
    export_html.add_argument("-o", "--output")

    return parser


COMMANDS = {
    "lint": cmd_lint,
    "analyze": cmd_analyze,
    "paths": cmd_paths,
    "play": cmd_play,
    "dot": cmd_dot,
    "html": cmd_html,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        text = Path(args.file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"gamebook: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2
    story, issues = parse(text)
    return COMMANDS[args.command](args, story, issues)


if __name__ == "__main__":
    sys.exit(main())
