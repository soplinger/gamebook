# Gamebook

**Lint, map, play and export branching interactive fiction from one plain-text file.**

Gamebook checks a choose-your-own-adventure story before you publish it. It flags broken links and dead ends with line numbers, finds passages nobody can reach and loops that trap readers, works out the shortest route to each ending, and plays the story with scripted choices. It exports a Graphviz graph or a single self-contained HTML page that readers can play in a browser. It runs offline, uses only the Python standard library, and keeps your whole story in one `.gb` file.

## Features

- **Plain-text format.** Passages are `@passage` blocks, choices are `- label -> target` lines, and everything else is prose. `@title`, `@start` and `ending` attributes cover the rest.
- **Line-numbered lint.** Reports unknown directives, malformed choices, invalid IDs, duplicate passages (with the line of the first definition), and unknown attributes, each as `file:line: error: ...`.
- **Broken-link checks.** Every choice must point at a passage that exists, and each error names the line of the choice that is wrong.
- **Dead-end and ending checks.** A passage with no choices must be marked `ending`, and an ending must not have choices.
- **Reachability warnings.** Passages unreachable from the start are reported, and so is a story with no reachable ending.
- **Duplicate choice and empty passage warnings.** Repeated choices and passages with no prose are flagged.
- **Story analysis.** Passage, ending and choice counts, total words, estimated reading time at a chosen speed (`--wpm`), and the list of unreachable passages.
- **Loop detection.** Every loop reachable from the start is printed, so you can see where a reader could circle forever.
- **Shortest route to each ending.** Shows the exact choices, the number of steps, the words read along the way and the reading time.
- **Simple-route counting.** Counts routes from start to an ending that never revisit a passage, stopping at `--limit` so large stories stay fast.
- **Scripted playthroughs.** Play with choice numbers or labels, see the text at every step, and get clear errors for bad or unused picks.
- **Graphviz export.** Writes a DOT graph with endings drawn as double circles and the start passage in bold.
- **Playable HTML export.** Produces one self-contained page with inline CSS and JavaScript, Back and Restart buttons, and safe embedding of story text. It makes no network requests.
- **JSON output and CI exit codes.** `lint --json` and `analyze --json` print machine-readable output. `lint` exits 1 on errors, and `lint --strict` also fails on warnings.

## Quick start

The repository includes `examples/lighthouse.gb`, a small story with three endings and two loops.

```sh
python3 gamebook.py lint examples/lighthouse.gb
# ok: 11 passage(s), no issues

python3 gamebook.py analyze examples/lighthouse.gb
python3 gamebook.py paths examples/lighthouse.gb
python3 gamebook.py play examples/lighthouse.gb 1 "Read the tide table" 1 1 1 1
python3 gamebook.py html examples/lighthouse.gb -o lighthouse.html
python3 gamebook.py dot examples/lighthouse.gb -o lighthouse.dot
```

Open `lighthouse.html` in any browser to read the story.

### Writing a story

```text
@title The Lighthouse at Gull Point
@start gate

@passage gate
The keeper's gate creaks open onto a wet, grey headland.
- Take the cliff path to the tower -> tower_door
- Walk back to the village -> village_end

@passage village_end ending
You never learn what the light was for.
```

Lines starting with `#` are comments. Passage IDs may contain letters, digits, `_` and `-`. If there is no `@start`, the first passage is the start.

## Commands

| Command | What it does |
| --- | --- |
| `lint FILE [--strict] [--json]` | Check the story. Exit 0 when clean, 1 on errors (or warnings with `--strict`), 2 when the file cannot be read. |
| `analyze FILE [--wpm N] [--json]` | Summarise counts, words, reading time, reachability and loops. |
| `paths FILE [--wpm N] [--limit N]` | Shortest route to each ending, plus the number of simple routes. |
| `play FILE [CHOICE ...]` | Walk the story. Each CHOICE is a number or a label, in order. |
| `dot FILE [-o OUT]` | Export a Graphviz DOT graph. |
| `html FILE [-o OUT]` | Export a playable, self-contained HTML page. |

`analyze`, `paths`, `play`, `dot` and `html` stop with the lint errors if the story has any. Warnings are printed to stderr and do not stop them.

## Running the tests

Install pytest, then run from the project root:

```sh
python3 -m pip install pytest
python3 -m pytest -q
```

The tests cover parsing and every lint rule with line numbers, reachability, loops, shortest routes, path counting with limits, playthroughs and their errors, the DOT and HTML exports (including checking that the HTML parses and that story text cannot break out of its script block), and the CLI exit codes. They use `tmp_path` for every file they write.

## Project layout

```text
gamebook.py            the tool: parser, lint, analysis, playthroughs, exports and CLI
examples/lighthouse.gb a sample story
tests/test_gamebook.py pytest suite
pytest.ini             test configuration
```

## Requirements

Python 3.10 or newer. The tool itself uses only the standard library. Pytest is needed only to run the tests.
