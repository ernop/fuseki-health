# fuseki-health

External health monitor for [fuseki.net](https://fuseki.net/). GitHub Actions
runs [`monitor.py`](monitor.py) every 5 minutes (GitHub may start scheduled runs
late when it is busy) and emails the site's owner when something breaks, every
6 hours while it stays broken, and once when it recovers.

Checks:

- **availability**: the homepage and feed answer with their content, and the
  private editor route answers with its login redirect. Checked from outside,
  so an outage of the whole server is reported too.
- **disk**: at least 20 GiB free on the server's root filesystem, read over SSH
  with a key that can run only `df -B1 --output=avail,size /`.

[`status.json`](status.json) is the public record of each check's state:
`up` or `down`, when it changed, when the owner was last notified, and the
problem while down. It changes only when a state changes (and once a month,
which keeps GitHub from disabling the schedule after 60 quiet days), so its
commit history is the outage history. The run logs are public too; the editor
path and every credential are masked.

This repository is public because GitHub Actions minutes are free for public
repositories; the private Fuseki repository would pay for a 5-minute schedule.

## Configuration

One environment secret, `FUSEKI_HEALTH_CONFIG` in environment `alerts`: JSON
with `smtp_login`, `smtp_password` (a Gmail app password), `from_email`,
`to_email`, `editor_prefix`, and `disk_reader_key` (private key of the
`diskread` account, provisioned by the Fuseki repository's
`scripts/provision_disk_reader.sh`). `known_hosts` pins the server's host key.

## Tests

```bash
python3 -m unittest test_monitor
```

The workflow runs them before every check; a failing run is itself emailed by
GitHub to the account that last changed the schedule.
