# Running the application

## Layout

All modules import each other as flat top-level modules, so they must sit in
one directory together with `column_map.json`:

```
app/
  lambda_handler.py     FastAPI app + Mangum entry point
  athena_filter.py      the query path for /filter_multiple_values (Athena)
  models.py             request models for /filter_multiple_values
  column_registry.py    column allowlist -- imported by both layers
  logger.py             unchanged from your original
  column_map.json       required
  column_map_alias.json optional: table.column for columns outside the base table
  requirements.txt
```

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env      # fill in Athena database/table/output location
set -a && source .env && set +a

python lambda_handler.py
```

Serves on `http://localhost:8000`. Interactive docs at `/docs`.

Equivalent, with auto-reload while editing:

```bash
uvicorn lambda_handler:app --reload --port 8000
```

Local runs need AWS credentials with `athena:StartQueryExecution`,
`athena:GetQueryExecution`, `athena:GetQueryResults`, and S3 read/write on the
`ATHENA_OUTPUT_LOCATION` bucket (Athena stages results there), plus Glue
catalog read access for the database/table configured.

### Verify

```bash
curl localhost:8000/health
curl localhost:8000/columns | head -c 300

curl -X POST localhost:8000/filter_multiple_values \
  -H 'Content-Type: application/json' \
  -d '{"current_column_name":"AGE_GROUP",
       "previous_filters":[{"column_name":"COUNTRY","values":["INDIA"]}],
       "limit":50}'
```

`/health`, `/config`, `/columns`, `/columns/describe`, and `/search` are served
entirely from `column_map.json` (local file or S3) and work with no query
engine at all -- useful for frontend development against a laptop with no AWS
access. `/filter_multiple_values` is the only endpoint that talks to Athena;
it returns `503 AthenaQueryFailed` or `503 AthenaQueryTimeout` when the query
engine is unavailable or too slow.

Every `/filter_multiple_values` response carries `source` and `elapsedMs`:

```json
{"column":"AGE_GROUP","values":["ADULT"],"source":"athena","elapsedMs":842.1}
```

### Reading the startup log

The registry reports data problems on every boot, since `column_map.json` is
loaded once at import time and a malformed map fails the cold start loudly
rather than on the first request. From your current `column_map.json`:

```
ERROR   Rejected 2 column_map entries that are not valid SQL identifiers:
        ['GEST_PERIOD_ GEST_PERIOD_UNIT', 'LinkedCase Count']
WARNING Column LOCAL_COMMENT is mapped to 2 widget params
INFO    Column registry ready: 488 columns (2 rejected, 1 param collisions)
```

Every column is treated as filterable; there is no schema metadata backing
the registry, so an incompatible column will fail at Athena query time rather
than being rejected up front with a clean 400.

## Deploy to Lambda

### Package

```bash
pip install -r requirements.txt -t package/
cp *.py column_map.json column_map_alias.json package/
cd package && zip -r ../function.zip . -x '*__pycache__*' && cd ..

aws lambda update-function-code \
  --function-name argus-cpd-api \
  --zip-file fileb://function.zip
```

If the zip exceeds 50MB, put the dependencies in a Lambda layer and ship only
your `.py` files and JSON in the function package.

### Configuration

```bash
aws lambda update-function-configuration \
  --function-name argus-cpd-api \
  --handler lambda_handler.handler \
  --timeout 30 \
  --memory-size 1024 \
  --environment 'Variables={
      ATHENA_DATABASE=kms_sdh_analytics,
      ATHENA_TABLE=sv_golden_layer,
      ATHENA_OUTPUT_LOCATION=s3://kms-rds-analytics/athena-results/,
      ALLOWED_DOMAIN=https://your-frontend.example.com,
      S3_BUCKET_NAME=argus-cpd-dashboard-web-859217211726,
      AWS_ACCOUNT_ID=<acct>,
      DASHBOARD_ID=<id>,
      QUICKSIGHT_USER_ARN=<arn>
  }'
```

Two things that are easy to get wrong:

- **Handler must be `lambda_handler.handler`.** The old string
  `lambda_handler.lamdba_handler` pointed at a bare FastAPI object, not a
  Mangum adapter.
- **Timeout 30s** to sit just under API Gateway's 29s limit, so you get a
  logged error rather than a silent gateway timeout.

### IAM

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow",
     "Action": ["athena:StartQueryExecution", "athena:GetQueryExecution",
                "athena:GetQueryResults", "athena:StopQueryExecution"],
     "Resource": "*"},
    {"Effect": "Allow",
     "Action": ["glue:GetTable", "glue:GetDatabase", "glue:GetPartitions"],
     "Resource": "*"},
    {"Effect": "Allow",
     "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
     "Resource": ["arn:aws:s3:::kms-rds-analytics",
                  "arn:aws:s3:::kms-rds-analytics/athena-results/*"]},
    {"Effect": "Allow",
     "Action": ["s3:GetObject", "s3:PutObject"],
     "Resource": "arn:aws:s3:::argus-cpd-dashboard-web-*/bookmarks/*"},
    {"Effect": "Allow",
     "Action": ["quicksight:GenerateEmbedUrlForRegisteredUser"],
     "Resource": "*"}
  ]
}
```

### Smoke test after deploy

```bash
curl https://<api-id>.execute-api.us-east-1.amazonaws.com/prod/health
```

A `200` with `"status":"ok"` means the registry loaded. A cold start error
usually means `column_map.json` failed to load from S3 or is malformed --
check `S3_BUCKET_NAME` and `COLUMN_MAP_KEY`.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `ModuleNotFoundError: column_registry` | Modules split across directories. They must be flat and together. |
| `Column registry is empty` | `column_map.json` missing or not next to the code. |
| `503 AthenaQueryFailed` | Check the Athena query execution's `StateChangeReason` in CloudWatch/Athena console -- usually a schema mismatch or missing table. |
| `503 AthenaQueryTimeout` | Query took longer than `ATHENA_QUERY_TIMEOUT_SECONDS`. Narrow the filters or raise the timeout. |
| `400 UnknownColumn` | The column name isn't in `column_map.json`. |
