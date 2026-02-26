import json
import os
TABLE_NAME = os.environ.get("TABLE_NAME", "ServiceReports")

import decimal
import boto3
import logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource("dynamodb")
TABLE_NAME = os.environ.get("TABLE_NAME", "ServiceReports")
table = dynamodb.Table(TABLE_NAME)


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, decimal.Decimal):
            return float(obj)
        return super().default(obj)


def build_response(status_code: int, body: dict | list):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "OPTIONS,GET,POST",
        },
        "body": json.dumps(body, cls=DecimalEncoder),
    }


def get_method_and_path(event):
    """
    Support both:
      - HTTP API / Lambda URL (payload v2): requestContext.http.method, rawPath
      - REST API (payload v1): httpMethod, path
    """
    request_context = event.get("requestContext") or {}
    http = request_context.get("http") or {}

    method = (
        http.get("method") or
        event.get("httpMethod") or
        request_context.get("httpMethod")
    )

    path = (
        event.get("rawPath") or  # HTTP API / Lambda URL
        event.get("path") or     # REST API
        ""
    )

    return (method or "").upper(), path


def lambda_handler(event, context):
    # DEBUG: log the full event so we can see exactly what API Gateway is sending
    print("Incoming event:", json.dumps(event))

    method, path = get_method_and_path(event)

    # Handle preflight
    if method == "OPTIONS":
        return build_response(200, {"message": "ok"})

    # ---------- POST /report ----------
    if method == "POST" and path.endswith("/report"):
        try:
            body_raw = event.get("body") or "{}"
            body = json.loads(body_raw)
        except json.JSONDecodeError:
            return build_response(400, {"error": "Invalid JSON body"})

        job_id = body.get("jobId")
        if not job_id:
            return build_response(400, {"error": "jobId is required in the body"})

        try:
            table.put_item(Item=body)
        except Exception as e:
            print("DynamoDB put_item error:", e)
            return build_response(500, {"error": "Failed to save report"})

        return build_response(200, {"message": "Report saved", "jobId": job_id})

    # ---------- GET /report/{jobId} ----------
    if method == "GET" and "/report/" in path:
        # supports /report/{jobId}
        job_id = path.split("/report/", 1)[-1]
        if not job_id:
            return build_response(400, {"error": "jobId missing in path"})

        try:
            resp = table.get_item(Key={"jobId": job_id})
        except Exception as e:
            print("DynamoDB get_item error:", e)
            return build_response(500, {"error": "Failed to fetch report"})

        item = resp.get("Item")
        if not item:
            return build_response(404, {"error": "Report not found", "jobId": job_id})

        return build_response(200, item)

    # ---------- Fallback ----------
    return build_response(405, {"error": "Method not allowed", "method": method, "path": path})