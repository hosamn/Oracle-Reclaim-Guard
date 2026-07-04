# Oracle Reclaim Guard

A cron-run script that checks whether any running OCI compute instances in a
compartment look "idle" by Oracle's own Always Free reclamation rules, and
sends a Telegram report.

## What it checks

Oracle will reclaim an Always Free compute instance if, over a **trailing
7-day window**, all of the following are true at once:

- CPU utilization, 95th percentile, **< 20%**
- Network utilization, 95th percentile, **< 20%**
- Memory utilization, mean, **< 20%** (A1/Ampere shapes only — memory isn't
  part of the criteria for non-A1 shapes)

This is an **AND** across all three, not an OR — an instance idling on CPU
but pushing real network traffic is not at risk. This script mirrors that
logic exactly (see `CPU_IDLE_THRESHOLD`, `MEM_IDLE_THRESHOLD`,
`NET_IDLE_THRESHOLD` in `oracle_reclaim_guard.py`).

Thresholds were confirmed against Oracle's Always Free documentation as
current (last checked July 2026). Oracle has changed these numbers before
(10% → 15% → 20% across past policy revisions), so if reclamation emails
don't match what this script reports, check Oracle's docs for a threshold
change before assuming a bug:
https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm

**Important:** this script is an independent recomputation of Oracle's
published formula, not a read of Oracle's actual internal verdict. There is
no public API that exposes "Oracle has flagged this instance as idle" — this
is a best-effort proxy using the same rules Oracle says it uses.

## How it works

1. Lists all `RUNNING` instances in `COMPARTMENT_ID`. If
   `INCLUDE_SUBCOMPARTMENTS=true`, it first recursively walks the
   compartment tree under `COMPARTMENT_ID` (via `IdentityClient.list_compartments`,
   one level at a time — this works at any depth, not just from the tenancy
   root) and lists instances in every descendant compartment too, merging
   the results.
2. For each instance, finds its primary VNIC.
3. Queries OCI Monitoring over the last 7 days at 1h resolution:
   - `CpuUtilization` (P95) — namespace `oci_computeagent`
   - `MemoryUtilization` (mean) — namespace `oci_computeagent`, A1 shapes only
   - `VnicToNetworkBytes` / `VnicFromNetworkBytes` (summed, converted to
     bytes/sec, P95'd, expressed as % of a configurable max bandwidth) —
     namespace `oci_vcn`
4. Flags `at_risk` if all three conditions are idle.
5. Prints and sends a plain-text report via Telegram, then exits with a
   status reflecting the outcome (see Exit codes below).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Check completed, no instance is at risk |
| `1` | Check could not complete — setup, config, or OCI listing failed |
| `2` | Check completed, at least one instance is at risk |

A `1` means the tool itself didn't get to run properly (bad OCI config,
permissions, network issue, etc.) — treat it differently from `0`/`2` in
any cron-level alerting, since it tells you nothing about instance risk.

## Reliability

The script is written so that a failure should always still produce a
printed/logged report and a best-effort Telegram message, rather than
crashing silently:

- Failures during OCI setup or instance listing are caught, reported, and
  result in exit code `1` (instead of an unhandled traceback with no
  notification at all).
- Failures for an individual instance during the check loop are caught and
  recorded as an `error` entry in that instance's report line, without
  stopping the run for the rest of the fleet.
- Telegram delivery itself goes through `safe_telegram_send()`, which
  swallows delivery failures (bad token, bad chat ID, Telegram API being
  down) and falls back to printing to stdout/log — so a broken notification
  channel can't take down the exit code or the rest of the run.

## Setup

### Requirements

```
pip install -r requirements.txt
```

### `.env` file

| Variable | Required | Description |
|---|---|---|
| `COMPARTMENT_ID` | Yes | OCID of the compartment to scan |
| `OCI_CONFIG_PROFILE` | No | OCI CLI config profile name (default: `DEFAULT`) |
| `REGION` | No | Overrides the region from the OCI config file |
| `INCLUDE_SUBCOMPARTMENTS` | No | `true`/`false` (default: `true`) |
| `BOT_TOKEN` | Yes (for alerts) | Telegram bot token |
| `Chat_UID` | Yes (for alerts) | Telegram chat ID to send the report to |
| `DEFAULT_MAX_MBITS` | No | Max bandwidth in Mbps used for network % calc (default: `50`) |
| `MAX_MBITS_<SHAPE>` | No | Per-shape override, e.g. `MAX_MBITS_VM_STANDARD_A1_FLEX=1000` |

### IAM policy

The identity running this script (user API key or instance principal) needs
read access to:

- `instances`
- `vnic-attachments`
- `metrics`
- `compartments` (only needed if `INCLUDE_SUBCOMPARTMENTS=true`)

in the target compartment.

### OCI config file

Separately from `.env`, the OCI Python SDK needs its own credentials file to
authenticate — this is what `load_config()` reads via `oci.config.from_file()`.
By default it looks at `~/.oci/config` (the home directory of **whichever
user actually runs the script** — see the cron note below), using the
`DEFAULT` profile unless `OCI_CONFIG_PROFILE` in `.env` says otherwise.

Format (INI-style, one `[PROFILE_NAME]` section per profile):

```ini
[DEFAULT]
user=ocid1.user.oc1..<your_user_ocid>
fingerprint=<your_api_key_fingerprint>
key_file=~/.oci/oci_api_key.pem
tenancy=ocid1.tenancy.oc1..<your_tenancy_ocid>
region=us-ashburn-1
```

| Field | Where to get it |
|---|---|
| `user` | Console → Profile menu → User settings → the OCID shown there |
| `fingerprint` | Shown when you upload/view the API key in Console → User settings → API Keys |
| `key_file` | Path to the **private** half of an API signing key pair you generate (PEM format, not the SSH key you use to log into instances) |
| `tenancy` | Console → Profile menu → Tenancy: `<name>` → the OCID shown there |
| `region` | Your home region, e.g. `us-ashburn-1` — only needed here if you don't set `REGION` in `.env` instead |

`pass_phrase=` is optional, only needed if your private key itself is
passphrase-protected.

**Cron gotcha:** if this runs under a cron job for a specific system user
(as opposed to your own interactive shell), make sure `~/.oci/config` and
the `key_file` it points to actually exist under *that* user's home
directory — `~` resolves differently for cron than for an interactive
terminal, and a config file that "exists" when you test the script by hand
may not be visible to the cron job at all. Using an absolute path for
`key_file` avoids one layer of this ambiguity.

Edit the path in `Cron` and install it:

```
0 9 * * * cd /path/to/script && /usr/bin/python3 oracle_reclaim_guard.py >> /var/log/oracle_reclaim_guard.log 2>&1
```

## Known limitations

- **Single compartment assumption.** This setup currently keeps instances
  and their subnets in the same compartment. That matters because OCI
  documents VNIC-level metrics (`oci_vcn` namespace) as living in the
  **subnet's** compartment, not the instance's compartment. The script's
  network query in `check_instance()` currently passes
  `instance.compartment_id`. If instances and subnets are ever split across
  compartments, that network query would silently return no data for
  affected instances — `network_util_p95` would come back `None`,
  `network_idle` would default to `False`, and since risk is an AND across
  all three metrics, those instances could never be flagged `at_risk` even
  if genuinely idle. This case is now visible in the report as `NO DATA`
  (see below) rather than looking identical to a busy instance, but it's
  still worth knowing the underlying cause if you ever see it. **If the
  compartment layout ever changes, `check_instance()` needs to look up the
  subnet's compartment for the network query specifically.**
- **Missing metric data is reported, not just defaulted.** If CPU, memory,
  or network data can't be retrieved for any reason, the corresponding
  metric is shown as `NO DATA` in the report (distinct from `OK`/`IDLE`),
  and the summary line at the top shows an `Incomplete data: N` count when
  this happens. The underlying idle flag still defaults to "not idle" on
  missing data (the safe direction — an instance is never flagged `at_risk`
  based on absent evidence), but it's no longer indistinguishable from a
  genuinely busy instance in the output.
- **Dependencies are unpinned.** `requirements.txt` doesn't pin versions,
  so a fresh install could pull SDK changes without warning.
