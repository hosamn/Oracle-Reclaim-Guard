#!/usr/bin/env python3

import os
import math
import datetime as dt
import requests
import oci

from dotenv import load_dotenv

CPU_IDLE_THRESHOLD = 20.0
MEM_IDLE_THRESHOLD = 20.0
NET_IDLE_THRESHOLD = 20.0

def parse_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")

def utc_now():
    return dt.datetime.now(dt.timezone.utc)

def percentile(values, p):
    values = sorted([
        float(v)
        for v in values
        if v is not None and not (isinstance(v, float) and math.isnan(v))
    ])

    if not values:
        return None

    if len(values) == 1:
        return values[0]

    k = (len(values) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)

    if f == c:
        return values[int(k)]

    d0 = values[f] * (c - k)
    d1 = values[c] * (k - f)
    return d0 + d1

def average(values):
    values = [
        float(v)
        for v in values
        if v is not None and not (isinstance(v, float) and math.isnan(v))
    ]

    if not values:
        return None

    return sum(values) / len(values)

def fmt_pct(value):
    if value is None:
        return "n/a"
    return f"{value:.2f}%"

def telegram_send(bot_token, chat_id, text):
    if not bot_token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=30,
    )

    response.raise_for_status()

def safe_telegram_send(bot_token, chat_id, text):
    try:
        telegram_send(bot_token, chat_id, text)
    except Exception as e:
        print(f"Telegram delivery failed: {e}")

def load_config():
    profile = os.getenv("OCI_CONFIG_PROFILE", "DEFAULT")
    region = os.getenv("REGION")

    config = oci.config.from_file(profile_name=profile)

    if region:
        config["region"] = region

    return config

def list_all_compartments(identity_client, root_compartment_id):
    """Recursively collect every active descendant compartment OCID under
    root_compartment_id. Works at any level of the hierarchy (not just the
    tenancy root), unlike list_compartments' compartment_id_in_subtree flag,
    which only applies when called on the root compartment."""
    all_ids = []
    to_visit = [root_compartment_id]

    while to_visit:
        current = to_visit.pop()

        response = oci.pagination.list_call_get_all_results(
            identity_client.list_compartments,
            compartment_id=current,
            lifecycle_state="ACTIVE",
        )

        children = response.data or []

        for child in children:
            all_ids.append(child.id)
            to_visit.append(child.id)

    return all_ids

def list_running_instances(compute_client, identity_client, compartment_id, include_subcompartments):
    compartment_ids = [compartment_id]

    if include_subcompartments:
        compartment_ids.extend(list_all_compartments(identity_client, compartment_id))

    all_instances = []

    for cid in compartment_ids:
        response = oci.pagination.list_call_get_all_results(
            compute_client.list_instances,
            compartment_id=cid,
            lifecycle_state="RUNNING",
        )

        all_instances.extend(response.data or [])

    return all_instances

def get_primary_vnic_id(compute_client, instance):
    response = oci.pagination.list_call_get_all_results(
        compute_client.list_vnic_attachments,
        compartment_id=instance.compartment_id,
        instance_id=instance.id,
    )

    attachments = response.data or []

    if not attachments:
        return None

    primary = None

    for attachment in attachments:
        if getattr(attachment, "is_primary", False):
            primary = attachment
            break

    if primary is None:
        primary = attachments[0]

    return getattr(primary, "vnic_id", None)

def query_metric_values(
    monitoring_client,
    compartment_id,
    namespace,
    query,
    start_time,
    end_time,
    resolution="1h",
):
    details = oci.monitoring.models.SummarizeMetricsDataDetails(
        namespace=namespace,
        query=query,
        start_time=start_time,
        end_time=end_time,
        resolution=resolution,
    )

    response = monitoring_client.summarize_metrics_data(
        compartment_id=compartment_id,
        summarize_metrics_data_details=details,
    )

    values = []

    for metric_data in response.data or []:
        for point in metric_data.aggregated_datapoints or []:
            if point.value is not None:
                values.append(point.value)

    return values

def is_a1_shape(shape):
    shape = (shape or "").lower()
    return "a1" in shape

def get_max_mbits_for_instance(instance):
    """
    Simple default:
    - uses DEFAULT_MAX_MBITS for all instances.

    If you want per-shape overrides, add env vars like:
    MAX_MBITS_VM_STANDARD_A1_FLEX=1000
    MAX_MBITS_VM_STANDARD_E2_1_MICRO=50
    """
    default_value = float(os.getenv("DEFAULT_MAX_MBITS", "50"))

    shape_key = (instance.shape or "").upper()
    shape_key = shape_key.replace(".", "_").replace("-", "_")
    env_key = f"MAX_MBITS_{shape_key}"

    override = os.getenv(env_key)

    if override:
        return float(override)

    return default_value

def check_instance(monitoring_client, instance, vnic_id):
    end_time = utc_now()
    start_time = end_time - dt.timedelta(days=7)

    instance_id = instance.id
    instance_name = instance.display_name
    compartment_id = instance.compartment_id
    shape = instance.shape

    max_mbits = get_max_mbits_for_instance(instance)

    # CPU P95
    cpu_query = f'CpuUtilization[1h]{{resourceId = "{instance_id}"}}.percentile(0.95)'

    cpu_values = query_metric_values(
        monitoring_client=monitoring_client,
        compartment_id=compartment_id,
        namespace="oci_computeagent",
        query=cpu_query,
        start_time=start_time,
        end_time=end_time,
    )

    cpu_p95 = percentile(cpu_values, 95)
    cpu_idle = cpu_p95 is not None and cpu_p95 < CPU_IDLE_THRESHOLD

    # Memory mean, A1 only
    memory_checked = is_a1_shape(shape)
    memory_mean = None
    memory_idle = True

    if memory_checked:
        memory_query = f'MemoryUtilization[1h]{{resourceId = "{instance_id}"}}.mean()'

        memory_values = query_metric_values(
            monitoring_client=monitoring_client,
            compartment_id=compartment_id,
            namespace="oci_computeagent",
            query=memory_query,
            start_time=start_time,
            end_time=end_time,
        )

        memory_mean = average(memory_values)
        memory_idle = memory_mean is not None and memory_mean < MEM_IDLE_THRESHOLD

    # Network P95 as % of max bandwidth
    network_util_p95 = None
    network_idle = False

    if vnic_id:
        inbound_query = f'VnicToNetworkBytes[1h]{{resourceId = "{vnic_id}"}}.sum()'
        outbound_query = f'VnicFromNetworkBytes[1h]{{resourceId = "{vnic_id}"}}.sum()'

        inbound_bytes_per_hour = query_metric_values(
            monitoring_client=monitoring_client,
            compartment_id=compartment_id,
            namespace="oci_vcn",
            query=inbound_query,
            start_time=start_time,
            end_time=end_time,
        )

        outbound_bytes_per_hour = query_metric_values(
            monitoring_client=monitoring_client,
            compartment_id=compartment_id,
            namespace="oci_vcn",
            query=outbound_query,
            start_time=start_time,
            end_time=end_time,
        )

        inbound_bytes_per_second = [x / 3600.0 for x in inbound_bytes_per_hour]
        outbound_bytes_per_second = [x / 3600.0 for x in outbound_bytes_per_hour]

        inbound_p95 = percentile(inbound_bytes_per_second, 95) or 0.0
        outbound_p95 = percentile(outbound_bytes_per_second, 95) or 0.0

        total_network_p95_bytes_per_second = inbound_p95 + outbound_p95

        max_bytes_per_second = (max_mbits * 1_000_000) / 8.0

        if max_bytes_per_second > 0:
            network_util_p95 = (
                total_network_p95_bytes_per_second / max_bytes_per_second
            ) * 100.0

            network_idle = network_util_p95 < NET_IDLE_THRESHOLD

    # Oracle-style AND logic
    at_risk = cpu_idle and network_idle and memory_idle

    data_incomplete = (
        cpu_p95 is None
        or (memory_checked and memory_mean is None)
        or network_util_p95 is None
    )

    return {
        "name": instance_name,
        "id": instance_id,
        "shape": shape,
        "vnic_id": vnic_id,
        "max_mbits": max_mbits,
        "cpu_p95": cpu_p95,
        "cpu_idle": cpu_idle,
        "memory_checked": memory_checked,
        "memory_mean": memory_mean,
        "memory_idle": memory_idle,
        "network_util_p95": network_util_p95,
        "network_idle": network_idle,
        "at_risk": at_risk,
        "data_incomplete": data_incomplete,
    }

def build_report(results):
    risky = [r for r in results if r["at_risk"]]
    safe = [r for r in results if not r["at_risk"]]
    incomplete = [r for r in results if r.get("data_incomplete")]

    lines = []
    lines.append("Oracle Reclaim Guard")
    lines.append("Window: last 7 days")
    lines.append(f"Instances checked: {len(results)}")
    lines.append(f"Safe: {len(safe)}")
    lines.append(f"At risk: {len(risky)}")
    if incomplete:
        lines.append(f"Incomplete data: {len(incomplete)} (see NO DATA entries below)")
    lines.append("")

    for r in results:
        status = "RISK" if r["at_risk"] else "SAFE"

        lines.append(f"{status} — {r['name']}")
        lines.append(f"Shape: {r['shape']}")

        cpu_label = "NO DATA" if r["cpu_p95"] is None else ("IDLE" if r["cpu_idle"] else "OK")
        lines.append(f"CPU P95: {fmt_pct(r['cpu_p95'])} — {cpu_label}")

        if r["memory_checked"]:
            memory_label = (
                "NO DATA" if r["memory_mean"] is None
                else ("IDLE" if r["memory_idle"] else "OK")
            )
            lines.append(f"Memory mean: {fmt_pct(r['memory_mean'])} — {memory_label}")
        else:
            lines.append("Memory: skipped, not A1")

        network_label = (
            "NO DATA" if r["network_util_p95"] is None
            else ("IDLE" if r["network_idle"] else "OK")
        )
        lines.append(
            f"Network P95: {fmt_pct(r['network_util_p95'])} "
            f"of {r['max_mbits']:.0f} Mbps — {network_label}"
        )

        lines.append("")

    return "\n".join(lines).strip()

def main():
    load_dotenv()

    compartment_id = os.getenv("COMPARTMENT_ID")
    include_subcompartments = parse_bool(os.getenv("INCLUDE_SUBCOMPARTMENTS"), True)

    bot_token = os.getenv("BOT_TOKEN")
    uid = os.getenv("Chat_UID")

    if not compartment_id:
        raise SystemExit("Missing COMPARTMENT_ID in .env")

    try:
        config = load_config()

        compute_client = oci.core.ComputeClient(config)
        monitoring_client = oci.monitoring.MonitoringClient(config)
        identity_client = oci.identity.IdentityClient(config)

        instances = list_running_instances(
            compute_client=compute_client,
            identity_client=identity_client,
            compartment_id=compartment_id,
            include_subcompartments=include_subcompartments,
        )
    except Exception as e:
        report = (
            "Oracle Reclaim Guard\n"
            "FAILED to complete the check (setup/listing error).\n"
            f"Error: {e}"
        )
        print(report)
        safe_telegram_send(bot_token, uid, report)
        raise SystemExit(1)

    if not instances:
        report = "Oracle Reclaim Guard\nNo running instances found."
        print(report)
        safe_telegram_send(bot_token, uid, report)
        return

    results = []

    for instance in instances:
        try:
            vnic_id = get_primary_vnic_id(compute_client, instance)

            result = check_instance(
                monitoring_client=monitoring_client,
                instance=instance,
                vnic_id=vnic_id,
            )

            results.append(result)

        except Exception as e:
            results.append({
                "name": instance.display_name,
                "id": instance.id,
                "shape": instance.shape,
                "vnic_id": None,
                "max_mbits": get_max_mbits_for_instance(instance),
                "cpu_p95": None,
                "cpu_idle": False,
                "memory_checked": is_a1_shape(instance.shape),
                "memory_mean": None,
                "memory_idle": False,
                "network_util_p95": None,
                "network_idle": False,
                "at_risk": False,
                "data_incomplete": True,
                "error": str(e),
            })

    report = build_report(results)

    errors = [r for r in results if "error" in r]
    if errors:
        report += "\n\nErrors:"
        for r in errors:
            report += f"\n- {r['name']}: {r['error']}"

    print(report)
    safe_telegram_send(bot_token, uid, report)

    if any(r["at_risk"] for r in results):
        raise SystemExit(2)

    raise SystemExit(0)

if __name__ == "__main__":
    main()
