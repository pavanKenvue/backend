import os
import re
import json
import time
import boto3

DATABASE = "kms_sdh_analytics"
OUTPUT_LOCATION = "s3://kms-rds-analytics/athena-results/"

TABLE_ALIAS_MAP = {
    "sv_golden_layer": "g",
    "sv_lab_details": "l",
    "sv_product_mapping": "p",
    "sv_med_non_med": "m",
}


def wait_for_query(athena, query_execution_id):
    while True:
        response = athena.get_query_execution(
            QueryExecutionId=query_execution_id
        )

        status = response["QueryExecution"]["Status"]["State"]

        if status == "SUCCEEDED":
            return

        if status in ("FAILED", "CANCELLED"):
            reason = response["QueryExecution"]["Status"].get(
                "StateChangeReason", ""
            )
            raise RuntimeError(
                f"Athena query {status}: {reason}"
            )

        time.sleep(2)


def normalize_data_type(data_type):
    """
    Convert:
      decimal(38,10) -> decimal
      varchar(255)   -> varchar
      char(20)       -> char
    Leave:
      bigint         -> bigint
      integer        -> integer
      timestamp      -> timestamp
    """
    return re.sub(r"\(.*\)$", "", data_type).lower()


def build_column_map():
    athena = boto3.client("athena")

    query = f"""
    SELECT
        table_name,
        column_name,
        data_type
    FROM information_schema.columns
    WHERE table_schema = '{DATABASE}'
      AND table_name IN (
          'sv_golden_layer',
          'sv_lab_details',
          'sv_product_mapping',
          'sv_med_non_med'
      )
    ORDER BY table_name, ordinal_position
    """

    response = athena.start_query_execution(
        QueryString=query,
        QueryExecutionContext={
            "Database": DATABASE
        },
        ResultConfiguration={
            "OutputLocation": OUTPUT_LOCATION
        }
    )

    query_execution_id = response["QueryExecutionId"]

    print(f"Running Athena query: {query_execution_id}")

    wait_for_query(athena, query_execution_id)

    column_map = {}
    column_type_map = {}

    next_token = None

    while True:
        params = {
            "QueryExecutionId": query_execution_id
        }

        if next_token:
            params["NextToken"] = next_token

        results = athena.get_query_results(**params)

        rows = results["ResultSet"]["Rows"]

        # Skip header row on first page
        if not next_token:
            rows = rows[1:]

        for row in rows:
            data = row.get("Data", [])

            table_name = (
                data[0].get("VarCharValue", "")
                if len(data) > 0 else ""
            )

            column_name = (
                data[1].get("VarCharValue", "")
                if len(data) > 1 else ""
            )

            raw_data_type = (
                data[2].get("VarCharValue", "")
                if len(data) > 2 else ""
            )

            data_type = normalize_data_type(raw_data_type)

            alias = TABLE_ALIAS_MAP.get(table_name)

            if not alias:
                continue

            column_info = {
                "table": table_name,
                "alias": alias,
                "column": column_name,
                "data_type": data_type
            }

            # Group by column name
            column_map.setdefault(column_name, []).append(
                column_info
            )

            # Alias.column -> datatype
            column_type_map[f"{alias}.{column_name}"] = data_type

        next_token = results.get("NextToken")

        if not next_token:
            break

    os.makedirs("resources", exist_ok=True)

    column_map_file = "resources/column_map_alias.json"
    type_map_file = "resources/column_type_map.json"

    with open(column_map_file, "w") as f:
        json.dump(column_map, f, indent=2)

    with open(type_map_file, "w") as f:
        json.dump(column_type_map, f, indent=2)

    print(
        f"Saved {len(column_map)} columns to {column_map_file}"
    )

    print(
        f"Saved {len(column_type_map)} datatype mappings to {type_map_file}"
    )

    return column_map, column_type_map


if __name__ == "__main__":
    build_column_map()