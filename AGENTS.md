# fuseki-health — agent notes

External health monitor for fuseki.net; see [README.md](README.md). The
server, its accounts and the product decisions live in the Fuseki repository
(`~/proj/fuseki4_ai`, private): its `docs/reliability.md` "Health monitoring"
section and `docs/todo.md` hold the decisions and open work.

- Public repository: never commit, print, or log a credential or the editor
  prefix. `availability_errors` redacts the prefix; the workflow masks every
  private value before the monitor runs.
- `status.json` must hold only values that change with a check's state
  (never free-space numbers or error text that varies per run); every change
  is a commit.
- Alert subjects and bodies are the owner's settled wording from the former
  on-server monitor; keep them unless the owner asks otherwise.
- Tests: `python3 -m unittest test_monitor` (stdlib only, no dependencies).
