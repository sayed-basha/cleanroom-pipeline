#!/usr/bin/env python3
"""
Create or update a BigQuery Analytics Hub data clean room, add a
privacy-safe (threshold-enforced) view, and grant publisher/subscriber
access — all via the official Google Cloud SDK, no Terraform involved.

Usage:
  python manage_clean_room.py \
      --clean-room-name patient_data \
      --project-id project-123456789 \
      --location asia-southeast1 \
      --dataset-id my_patient_dataset \
      --source-table patien_table \
      --privacy-unit-col patient_id \
      --threshold 50 \
      --publishers user:alice@gmail.com \
      --subscribers user:bob@gmail.com
"""

import argparse
import sys

from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud import bigquery
from google.cloud import bigquery_analyticshub_v1 as analyticshub
from google.iam.v1 import policy_pb2


def normalize_principal(value):
    """Accepts a plain email ('alice@company.com') and returns it prefixed as
    'user:alice@company.com'. If the value already has a recognized prefix
    (user:, group:, serviceAccount:, domain:), it's left untouched — this lets
    people just type their email like they're used to, while still allowing
    an explicit group:/serviceAccount: when that's actually needed."""
    if not value:
        return value
    known_prefixes = ("user:", "group:", "serviceAccount:", "domain:")
    if value.startswith(known_prefixes):
        return value
    return f"user:{value}"


def create_or_get_exchange(client, project_id, location, exchange_id, display_name):
    parent = f"projects/{project_id}/locations/{location}"
    name = f"{parent}/dataExchanges/{exchange_id}"
    try:
        exchange = analyticshub.DataExchange(
            display_name=display_name,
            description=f"Data clean room for {exchange_id}",
            sharing_environment_config=analyticshub.SharingEnvironmentConfig(
                dcr_exchange_config=analyticshub.SharingEnvironmentConfig.DcrExchangeConfig()
            ),
        )
        result = client.create_data_exchange(
            request={
                "parent": parent,
                "data_exchange_id": exchange_id,
                "data_exchange": exchange,
            }
        )
        print(f"  Created exchange: {result.name}")
        return result
    except AlreadyExists:
        result = client.get_data_exchange(request={"name": name})
        print(f"  Exchange already exists: {result.name}")
        return result


def create_or_replace_view(bq_client, project_id, dataset_id, view_name,
                            source_table, privacy_unit_col, threshold):
    view_ref = f"`{project_id}.{dataset_id}.{view_name}`"
    source_ref = f"`{project_id}.{dataset_id}.{source_table}`"
    privacy_policy_json = (
        '{"aggregation_threshold_policy": '
        f'{{"threshold": {threshold}, "privacy_unit_column": "{privacy_unit_col}"}}}}'
    )
    query = f"""
        CREATE OR REPLACE VIEW {view_ref}
        OPTIONS (privacy_policy = '''{privacy_policy_json}''')
        AS SELECT * FROM {source_ref}
    """
    job = bq_client.query(query)
    job.result()  # wait for completion, raises on error
    print(f"  Created/updated view: {project_id}.{dataset_id}.{view_name}")


def create_or_update_listing(client, project_id, location, exchange_id,
                              listing_id, dataset_id, view_name, display_name,
                              restrict_query_result=True):
    parent = f"projects/{project_id}/locations/{location}/dataExchanges/{exchange_id}"
    name = f"{parent}/listings/{listing_id}"
    listing = analyticshub.Listing(
        display_name=display_name,
        bigquery_dataset=analyticshub.Listing.BigQueryDatasetSource(
            dataset=f"projects/{project_id}/datasets/{dataset_id}",
            selected_resources=[
                analyticshub.Listing.BigQueryDatasetSource.SelectedResource(
                    table=f"projects/{project_id}/datasets/{dataset_id}/tables/{view_name}"
                )
            ],
        ),
        # IMPORTANT: this must be a plain dict, not
        # analyticshub.Listing.RestrictedExportConfig(...). Constructing it via
        # the class constructor silently drops restrict_query_result when the
        # request is sent — confirmed via debug logging against the live API.
        # The dict form is the reliable way to set this nested message.
        restricted_export_config={
            "enabled": True,
            "restrict_query_result": restrict_query_result,
        },
    )
    try:
        result = client.create_listing(
            request={
                "parent": parent,
                "listing_id": listing_id,
                "listing": listing,
            }
        )
        print(f"  Created listing: {result.name}")
        return result
    except AlreadyExists:
        listing.name = name
        result = client.update_listing(
            request={
                "listing": listing,
                "update_mask": {"paths": ["bigquery_dataset", "restricted_export_config"]},
            }
        )
        print(f"  Updated listing: {result.name}")
        return result


def grant_iam(client, resource_name, role, members, get_policy_fn, set_policy_fn):
    policy = get_policy_fn(request={"resource": resource_name})
    existing_members = set()
    for binding in policy.bindings:
        if binding.role == role:
            existing_members.update(binding.members)

    new_members = set(members) - existing_members
    if not new_members:
        print(f"  {role}: all members already granted")
        return

    found = False
    for binding in policy.bindings:
        if binding.role == role:
            binding.members.extend(new_members)
            found = True
            break
    if not found:
        policy.bindings.append(
            policy_pb2.Binding(role=role, members=list(new_members))
        )

    set_policy_fn(request={"resource": resource_name, "policy": policy})
    print(f"  {role}: granted to {new_members}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--clean-room-name", required=True)
    p.add_argument("--project-id", required=True)
    p.add_argument("--location", required=True)
    p.add_argument("--dataset-id", required=True)
    p.add_argument("--source-table", required=True)
    p.add_argument("--privacy-unit-col", required=True)
    p.add_argument("--threshold", type=int, required=True)
    p.add_argument("--publishers", nargs="*", default=[])
    p.add_argument("--subscribers", nargs="*", default=[])
    p.add_argument(
        "--allow-query-result-export",
        action="store_true",
        help="If set, subscribers CAN save/export query results. Default: disabled (safer).",
    )
    args = p.parse_args()

    # Auto-prefix plain emails with "user:" — lets people type just their
    # email address instead of needing to remember the user:/group: syntax.
    # Also drops any blank entries (e.g. when the field was left empty).
    args.publishers = [normalize_principal(m) for m in args.publishers if m]
    args.subscribers = [normalize_principal(m) for m in args.subscribers if m]

    exchange_id = f"cleanroom_{args.clean_room_name}"
    listing_id = f"listing_{args.clean_room_name}"
    view_name = f"cleanroom_view_{args.clean_room_name}"

    ah_client = analyticshub.AnalyticsHubServiceClient()
    bq_client = bigquery.Client(project=args.project_id)

    print(f"[1/4] Clean room exchange: {exchange_id}")
    create_or_get_exchange(
        ah_client, args.project_id, args.location, exchange_id,
        f"Clean Room - {args.clean_room_name}",
    )
    exchange_name = (
        f"projects/{args.project_id}/locations/{args.location}"
        f"/dataExchanges/{exchange_id}"
    )

    print(f"[2/4] Privacy-safe view: {view_name}")
    create_or_replace_view(
        bq_client, args.project_id, args.dataset_id, view_name,
        args.source_table, args.privacy_unit_col, args.threshold,
    )

    print(f"[3/4] Listing: {listing_id}")
    create_or_update_listing(
        ah_client, args.project_id, args.location, exchange_id, listing_id,
        args.dataset_id, view_name, f"Shared Data - {args.clean_room_name}",
        restrict_query_result=not args.allow_query_result_export,
    )
    listing_name = f"{exchange_name}/listings/{listing_id}"

    print("[4/4] IAM grants")
    if args.publishers:
        grant_iam(
            ah_client, exchange_name, "roles/analyticshub.publisher",
            args.publishers, ah_client.get_iam_policy, ah_client.set_iam_policy,
        )
    if args.subscribers:
        grant_iam(
            ah_client, exchange_name, "roles/analyticshub.subscriber",
            args.subscribers, ah_client.get_iam_policy, ah_client.set_iam_policy,
        )
        grant_iam(
            ah_client, listing_name, "roles/analyticshub.subscriber",
            args.subscribers, ah_client.get_iam_policy, ah_client.set_iam_policy,
        )

    print(f"\nDone. Clean room '{args.clean_room_name}' is ready.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
