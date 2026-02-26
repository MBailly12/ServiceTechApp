import json
import os
import uuid
import decimal
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# DynamoDB setup
TABLE_NAME = os.environ.get("TABLE_NAME", "ServiceReports")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)

# S3 setup
s3 = boto3.client("s3")
PHOTOS_BUCKET = os.environ.get("PHOTOS_BUCKET")


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, decimal.Decimal):
            return float(obj)
        return super().default(obj)


def build_response(status_code, body):
    """Standard API Gateway response with CORS headers."""
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
        http.get("method")
        or event.get("httpMethod")
        or request_context.get("httpMethod")
    )

    path = (
        event.get("rawPath")  # HTTP API / Lambda URL
        or event.get("path")  # REST API
        or ""
    )

    return (method or "").upper(), path


def handle_photo_url(event):
    """Generate a pre-signed S3 URL so the front end can upload a photo."""

    if not PHOTOS_BUCKET:
        return build_response(500, {"error": "PHOTOS_BUCKET not configured"})

    path_params = event.get("pathParameters") or {}
    job_id = path_params.get("jobId")

    if not job_id:
        return build_response(400, {"error": "jobId path parameter required"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        body = {}

    filename = (body.get("filename") or "photo.jpg").strip()
    content_type = body.get("contentType") or "image/jpeg"

    # Object key: group by jobId
    key = f"reports/{job_id}/{uuid.uuid4()}-{filename}"

    try:
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": PHOTOS_BUCKET,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=300,  # 5 minutes
        )
    except Exception as e:
        logger.exception("Failed to generate presigned URL")
        return build_response(
            500,
            {"error": f"Failed to generate upload URL: {str(e)}"},
        )

    return build_response(
        200,
        {
            "uploadUrl": upload_url,
            "objectKey": key,
            "photoLocation": f"s3://{PHOTOS_BUCKET}/{key}",
        },
    )


def lambda_handler(event, context):
    # DEBUG: log the full event so we can see exactly what API Gateway is sending
    logger.info("Incoming event: %s", json.dumps(event))

    method, path = get_method_and_path(event)

    # 1) Photo upload URL route: POST /report/{jobId}/photo-url
    if method == "POST" and path.startswith("/report/") and path.endswith("/photo-url"):
        return handle_photo_url(event)

    # 2) CORS preflight
    if method == "OPTIONS":
        return build_response(200, {"message": "ok"})

    # 3) Create / update report: POST /report
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
        except Exception:
            logger.exception("DynamoDB put_item error")
            return build_response(500, {"error": "Failed to save report"})

        return build_response(200, {"message": "Report saved", "jobId": job_id})

    # 4) Get report by ID: GET /report/{jobId}
    if method == "GET" and "/report/" in path:
        job_id = path.split("/report/", 1)[-1]
        if not job_id:
            return build_response(400, {"error": "jobId missing in path"})

        try:
            resp = table.get_item(Key={"jobId": job_id})
        except Exception:
            logger.exception("DynamoDB get_item error")
            return build_response(500, {"error": "Failed to fetch report"})

        item = resp.get("Item")
        if not item:
            return build_response(404, {"error": "Report not found", "jobId": job_id})

        return build_response(200, item)

    # 5) Fallback for unknown routes
    return build_response(
        405,
        {"error": "Method not allowed", "method": method, "path": path},
    )