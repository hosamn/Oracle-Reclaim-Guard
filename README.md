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

1. Lists all `RUNNING` instances in `COMPARTMENT_ID` (optionally including
   subcompartments).
2. For each instance, finds its primary VNIC.
3. Queries OCI Monitoring over the last 7 days at 1h resolution:
   - `CpuUtilization` (P95) — namespace `oci_computeagent`
   - `MemoryUtilization` (mean) — namespace `oci_computeagent`, A1 shapes only
   - `VnicToNetworkBytes` / `VnicFromNetworkBytes` (summed, converted to
     bytes/sec, P95'd, expressed as % of a configurable max bandwidth) —
     namespace `oci_vcn`
4. Flags `at_risk` if all three conditions are idle.
5. Prints and sends a plain-text report via Telegram. Exits `2` if any
   instance is at risk, `0` otherwise (crontab/log-friendly).

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
| `UID` | Yes (for alerts) | Telegram chat ID to send the report to |
| `DEFAULT_MAX_MBITS` | No | Max bandwidth in Mbps used for network % calc (default: `50`) |
| `MAX_MBITS_<SHAPE>` | No | Per-shape override, e.g. `MAX_MBITS_VM_STANDARD_A1_FLEX=1000` |

### IAM policy

The identity running this script (user API key or instance principal) needs
read access to:

- `instances`
- `vnic-attachments`
- `metrics`

in the target compartment.

### Cron

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
  if genuinely idle. No error would be thrown; it would just under-report
  silently. **If the compartment layout ever changes, `check_instance()`
  needs to look up the subnet's compartment for the network query
  specifically.**
- **Missing data defaults to "not idle."** If CPU, memory, or network data
  can't be retrieved for any reason, the corresponding `*_idle` flag
  defaults to `False` rather than `True`. This is deliberately the
  fail-safe direction (don't claim idle without evidence), but it means a
  data-retrieval problem looks identical to "instance is actively used" in
  the report — there's no separate "unknown" state surfaced to Telegram.
- **Dependencies are unpinned.** `requirements.txt` doesn't pin versions,
  so a fresh install could pull SDK changes without warning.
