# Agent Detective — the terminal half of the demo video.
#
#   brew install vhs && vhs demo/demo.tape
#
# Renders to demo/out/, which is gitignored: the tape is the source, the mp4 is
# a build artifact. That is the whole point of scripting the recording — when a
# release changes the wording of a verdict, re-render instead of re-shooting.
#
# The trace here is examples/diamond_eval.py, deliberately: it is English,
# needs no API key, and the deterministic contract check gives it 95%
# confidence. See the note at the bottom for the agent_topo_db variant.

Output demo/out/demo.mp4
Output demo/out/demo.gif

Set Shell "zsh"
# Sized so the ENTIRE verdict fits without scrolling. The header carries the
# confidence, and at 1600x1000/20pt it scrolled off the top before the hold —
# ~154 columns also stops the `supporting:` lines wrapping into mush.
Set FontSize 18
Set Width 1800
Set Height 1120
Set Padding 30
Set TypingSpeed 45ms
Set WindowBar Colorful

# Staging, off camera: put the workspace CLI on PATH and produce the faulted
# trace. The viewer should see `detective`, never `uv run --package ...`.
Hide
# Interactive zsh does NOT treat `#` as a comment unless this is set, so the
# narration lines below would otherwise run as commands and print
# "command not found: #" on camera.
Type "setopt interactive_comments"
Enter
Type "export PATH=$PWD/.venv/bin:$PATH"
Enter
Type "uv run python examples/diamond_eval.py --inject >/dev/null 2>&1"
Enter
Type "clear"
Enter
Show

Sleep 1500ms

# ── Beat 1 — five agents ran, nothing crashed, exit code was 0.
Type "# a press release: one extractor, two writers, one editor"
Sleep 1s
Enter
Sleep 500ms

Type "# nothing crashed. the copy reads fine. and a price is wrong."
Sleep 1500ms
Enter
Sleep 1s

# ── Beat 2 — the question the eval cannot answer: WHICH agent?
Type "detective analyze diamond_run.json"
Sleep 800ms
Enter
Sleep 8s

# Hold on the pipeline block. This is the shot: the node where the problem
# SURFACED is not the node that caused it.
Sleep 4s

# ── Beat 3 — hand the findings to a coding agent.
Type "clear"
Enter
Sleep 500ms

Type "detective analyze diamond_run.json --markdown > findings.md"
Sleep 800ms
Enter
Sleep 2s

Type "head -28 findings.md"
Sleep 500ms
Enter
Sleep 6s

# ── Beat 4 — the gate. Exit 1 is the product.
Type "clear"
Enter
Sleep 500ms

Type "detective analyze diamond_run.json > /dev/null; echo exit=$?"
Sleep 800ms
Enter
Sleep 3s

Hide
Type "rm -f findings.md"
Enter
Show

# ── Variant: a real recorded topology from agent_topo_db instead of the
# example. Swap the analyze target for:
#
#   corpus/traces/12_supervisor_multi_subagents__drop_numbers_at_changelog_agent.json
#
# Better story — a changelog agent silently drops all ten figures, so the
# deliverable says "z s na ms" — but two caveats. It is in Czech, and without
# JUDGE_BASE_URL/JUDGE_MODEL set it reports attribution 0%, which reads as
# broken on camera even though it is honest. Re-record that topology with an
# English task and the judge configured before using it.
