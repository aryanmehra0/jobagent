# AGENTS.md

See [`CLAUDE.md`](CLAUDE.md) — the orientation file for any AI coding agent
working in this repository, tool-agnostic despite the filename. Read it
first, then [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the verified
file-by-file map.

Asking whether *this project's own agent* (not the coding agent reading this
file) follows a real agent loop, uses system prompts, or does real LLM
tool-calling? That's [`docs/AGENT_DESIGN.md`](docs/AGENT_DESIGN.md) — the
Perceive/Reason/Memory/Plan/Act/Observe mapping, file by file, including
where system prompts are enforced and where Phase 7 uses native
function-calling (`get_profile_evidence`) instead of a single prompt-and-answer
call.
