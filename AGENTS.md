# Notes for agents

Jev (TypeSafe's decision model) plays Super Mario Bros., or a Flappy clone, inside a
microsandbox microVM. The host freezes copies of the running VM, and every death forks four
of them. A browser page shows the game and its branching timeline.

## Writing rules

These matter more than anything else in this file.

- **The README is for a first-time visitor.** What it is, how it works at a glance, how to
  run it. Keep it short. One idea per section, plain sentences, no parenthetical asides.
- **Markdown files are not a place to record your work.** Do not add changelogs, validation
  notes, measurements, UI manuals, option tables, implementation details, or the reasoning
  behind a decision. Reasoning goes in a code comment next to the code, or in the commit
  message. Findings go in your reply to the person you are working with.
- **Do not create new `.md` files or a `docs/` folder** unless you are asked to.
- When a change makes a README sentence wrong, fix that sentence. Do not add a paragraph.
- If you are about to write a third sentence about one feature, stop and cut.

## Layout

| Path | What lives there |
| --- | --- |
| `mnd/orchestrator.py` | The host loop: checkpoints, rewind target, forks, budgets (`Settings`) |
| `mnd/backend.py` | The microsandbox SDK adapter: create, branch, branch_many, pause, kill |
| `mnd/guest.py` | The game loop inside the VM: frames, Jev calls, gates |
| `mnd/policy.py`, `mnd/perception.py`, `mnd/terrain.py`, `mnd/pipe_control.py` | What Jev is asked and the state and hints it is given |
| `mnd/recovery.py` | The scripted sequences a fork tries |
| `mnd/games.py`, `mnd/bird.py`, `mnd/bird_guest.py` | The game list; the Flappy clone (engine, art, Jev's outlook, a fork's verses) and its loop inside the VM |
| `mnd/protocol.py`, `mnd/actions.py` | Gate protocol, the twelve controller moves |
| `mnd/web.py`, `mnd/replay.py`, `mnd/launcher.py` | Local web server, replay of recorded runs, `mnd` entry point |
| `web/` | The page: `app.js` (everything on screen, video export), `timeline.js`, `sound.js`, `style.css` |
| `host/` | One-off probes and the music loop maker |
| `diagrams/render.py` | Generates the README's light and dark SVGs |
| `runs/` | Recorded runs. Ignored by git |

## Working here

```sh
uv sync --extra dev
uv run python -m unittest discover -s tests
uv run ruff check mnd host tests && uv run ruff format --check mnd host tests
node --check web/app.js && node --test tests/timeline.test.cjs
uv run python diagrams/render.py      # after editing a diagram
uv run mnd                     # live; needs TYPESAFE_API_KEY, ffmpeg for video; Docker only with --build-image
uv run python -m mnd.web              # replays only; no key, no VMs
```

- Design and test the page against a **replay of a recorded run**, never the synthetic simulation.
- `web/app.js` is often edited by more than one session. Re-read the part you are changing right before you patch it.
- Sound never starts by itself and is never remembered across page loads.
- Secrets come from the environment or a git-ignored `.env`. Never write a key, a personal path, or a machine-specific default into the repo.
- Do not name the game's publisher or composers in music prompts, and do not commit ROMs.
