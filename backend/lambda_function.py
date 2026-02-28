"""
lambda_function.py

Single-file, production-ready AWS Lambda handler that:
- Writes/reads DynamoDB reports keyed by jobId.
- Generates Amazon S3 presigned upload parameters for direct-to-S3 uploads.

Event compatibility:
- API Gateway REST API (payload v1.0): event["httpMethod"], event["path"]
- API Gateway HTTP API / Lambda Function URL (payload v2.0):
  event["requestContext"]["http"]["method"], event["rawPath"]

Environment variables (requested):
- TABLE_NAME (default: "ServiceReports")
- PHOTOS_BUCKET (required for presign route)
- PHOTOS_BUCKET_REGION (optional, fallback unspecified)
- PRESIGN_MODE ("put" or "post", default: "put")
- PRESIGN_EXPIRES (seconds, default: 300)
- PRESIGN_SIGN_CONTENT_TYPE ("true"/"false", default: false)
- LOG_LEVEL (default: "INFO")

Optional environment variables (unspecified by user; included to support
common bucket policies requiring encryption headers):
- PRESIGN_SSE ("none", "AES256", "aws:kms", default: "none")
- PRESIGN_SSE_KMS_KEY_ID (only used if PRESIGN_SSE="aws:kms")

Optional CORS overrides (unspecified by user):
- CORS_ALLOW_ORIGIN (default: "*")
- CORS_ALLOW_HEADERS (default: common headers)
- CORS_ALLOW_METHODS (default: "OPTIONS,GET,POST")
"""

from __future__ import annotations

import base64
import decimal
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


# -----------------------------------------------------------------------------
# Logging (structured JSON logs)
# -----------------------------------------------------------------------------

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())


def log_json(level: int, message: str, **fields: Any) -> None:
    """
    Emit a single-line JSON log suitable for CloudWatch Logs filtering.
    """
    payload = {"message": message, **fields}
    LOGGER.log(level, json.dumps(payload, default=str))


# -----------------------------------------------------------------------------
# JSON encoding helpers (DynamoDB Decimal support)
# -----------------------------------------------------------------------------

class DecimalEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, decimal.Decimal):
            return float(obj)
        return super().default(obj)


def dumps(data: Any) -> str:
    return json.dumps(data, cls=DecimalEncoder)


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

_ALLOWED_PRESIGN_MODES = {"put", "post"}
_ALLOWED_SSE = {"none", "AES256", "aws:kms"}

# jobId constraints are an application choice (assumption) to keep paths clean.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

# MIME type validation to avoid header injection; fallback to default if invalid.
_CONTENT_TYPE_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")


@dataclass(frozen=True)
class AppConfig:
    table_name: str
    photos_bucket: Optional[str]
    photos_bucket_region: Optional[str]  # fallback unspecified when None

    presign_mode: str  # "put" or "post"
    presign_expires: int
    sign_content_type: bool

    # Optional (unspecified by user): SSE support for encryption-required buckets.
    presign_sse: str  # "none", "AES256", "aws:kms"
    presign_sse_kms_key_id: Optional[str]

    cors_allow_origin: str
    cors_allow_headers: str
    cors_allow_methods: str


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_config() -> AppConfig:
    table_name = os.environ.get("TABLE_NAME", "ServiceReports")

    photos_bucket = os.environ.get("PHOTOS_BUCKET")
    photos_bucket_region = os.environ.get("PHOTOS_BUCKET_REGION") or None

    presign_mode = (os.environ.get("PRESIGN_MODE", "put") or "put").strip().lower()
    if presign_mode not in _ALLOWED_PRESIGN_MODES:
        raise ValueError(
            f"Invalid PRESIGN_MODE={presign_mode!r}; expected 'put' or 'post'."
        )

    presign_expires = env_int("PRESIGN_EXPIRES", 300)
    if presign_expires <= 0:
        raise ValueError("PRESIGN_EXPIRES must be a positive integer.")

    sign_content_type = env_bool("PRESIGN_SIGN_CONTENT_TYPE", False)

    presign_sse = (os.environ.get("PRESIGN_SSE", "none") or "none").strip()
    if presign_sse not in _ALLOWED_SSE:
        raise ValueError(
            f"Invalid PRESIGN_SSE={presign_sse!r}; expected one of "
            f"{sorted(_ALLOWED_SSE)}."
        )

    kms_key_id = os.environ.get("PRESIGN_SSE_KMS_KEY_ID") or None
    if presign_sse != "aws:kms":
        kms_key_id = None

    cors_allow_origin = os.environ.get("CORS_ALLOW_ORIGIN", "*")
    cors_allow_headers = os.environ.get(
        "CORS_ALLOW_HEADERS",
        ",".join(
            [
                "Content-Type",
                "Authorization",
                "X-Requested-With",
                "X-Amz-Date",
                "X-Amz-Security-Token",
                "X-Amz-User-Agent",
                "x-amz-server-side-encryption",
                "x-amz-server-side-encryption-aws-kms-key-id",
            ]
        ),
    )
    cors_allow_methods = os.environ.get("CORS_ALLOW_METHODS", "OPTIONS,GET,POST")

    return AppConfig(
        table_name=table_name,
        photos_bucket=photos_bucket,
        photos_bucket_region=photos_bucket_region,
        presign_mode=presign_mode,
        presign_expires=presign_expires,
        sign_content_type=sign_content_type,
        presign_sse=presign_sse,
        presign_sse_kms_key_id=kms_key_id,
        cors_allow_origin=cors_allow_origin,
        cors_allow_headers=cors_allow_headers,
        cors_allow_methods=cors_allow_methods,
    )


def create_s3_client(config: AppConfig):
    """
    Create an S3 client with explicit signature version.
    Region is set only if PHOTOS_BUCKET_REGION is provided; otherwise fallback
    is unspecified and boto3 resolves region via its standard chain.
    """
    client_config = Config(signature_version="s3v4")

    if config.photos_bucket_region:
        return boto3.client(
            "s3",
            region_name=config.photos_bucket_region,
            config=client_config,
        )

    return boto3.client("s3", config=client_config)


def create_dynamodb_table(config: AppConfig):
    dynamodb = boto3.resource("dynamodb")
    return dynamodb.Table(config.table_name)


# -----------------------------------------------------------------------------
# HTTP response helpers (Lambda proxy style)
# -----------------------------------------------------------------------------

def cors_headers(config: AppConfig) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": config.cors_allow_origin,
        "Access-Control-Allow-Headers": config.cors_allow_headers,
        "Access-Control-Allow-Methods": config.cors_allow_methods,
    }


def response(
    config: AppConfig,
    status_code: int,
    body: Any,
    extra_headers: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    headers = cors_headers(config)
    if extra_headers:
        headers.update(dict(extra_headers))
    return {"statusCode": status_code, "headers": headers, "body": dumps(body)}


def error(
    config: AppConfig,
    status_code: int,
    code: str,
    message: str,
    request_id: Optional[str] = None,
    details: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"error": {"code": code, "message": message}}
    if request_id:
        payload["requestId"] = request_id
    if details:
        payload["error"]["details"] = dict(details)
    return response(config, status_code, payload)


# -----------------------------------------------------------------------------
# Event parsing (REST v1, HTTP API v2, Lambda Function URL v2)
# -----------------------------------------------------------------------------

def get_request_id(event: Mapping[str, Any], context: Any) -> Optional[str]:
    rc = event.get("requestContext") or {}
    if isinstance(rc, dict) and rc.get("requestId"):
        return str(rc["requestId"])
    aws_req_id = getattr(context, "aws_request_id", None)
    if aws_req_id:
        return str(aws_req_id)
    return None


def get_method(event: Mapping[str, Any]) -> str:
    rc = event.get("requestContext") or {}
    http = (rc.get("http") if isinstance(rc, dict) else None) or {}
    method = http.get("method") or event.get("httpMethod") or rc.get("httpMethod")
    return (method or "").upper()


def get_path(event: Mapping[str, Any]) -> str:
    path = event.get("rawPath") or event.get("path") or ""
    if not isinstance(path, str):
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


def get_path_params(event: Mapping[str, Any]) -> Dict[str, str]:
    params = event.get("pathParameters") or {}
    if not isinstance(params, dict):
        return {}
    return {str(k): str(v) for k, v in params.items() if v is not None}


def parse_json_body(event: Mapping[str, Any]) -> Dict[str, Any]:
    body = event.get("body")
    if body is None or body == "":
        return {}

    if event.get("isBase64Encoded") is True:
        try:
            body_bytes = base64.b64decode(body)
            body = body_bytes.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            raise ValueError("Invalid base64 request body") from exc

    if not isinstance(body, str):
        raise ValueError("Request body must be a string")

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid JSON body") from exc

    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError("JSON body must be an object")
    return parsed


# -----------------------------------------------------------------------------
# Validation & sanitization
# -----------------------------------------------------------------------------

def validate_job_id(job_id: str) -> str:
    value = (job_id or "").strip()
    if not value:
        raise ValueError("jobId is required")
    if not _JOB_ID_RE.fullmatch(value):
        raise ValueError(
            "jobId must match pattern [A-Za-z0-9][A-Za-z0-9_-]{0,127}"
        )
    return value


def sanitize_filename(filename: str) -> str:
    """
    Sanitize a client-provided filename for safe inclusion in an S3 key.
    """
    name = (filename or "").strip() or "photo.jpg"
    # Remove any path components (Windows/Unix).
    name = name.replace("\\", "/").split("/")[-1]
    # Keep a conservative character set.
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    name = name.lstrip(".") or "photo.jpg"
    return name[:120]


def sanitize_content_type(content_type: str) -> str:
    """
    Validate content type, defaulting to image/jpeg if invalid or missing.
    """
    ct = (content_type or "").strip()
    if not ct or len(ct) > 127:
        return "image/jpeg"
    if _CONTENT_TYPE_RE.fullmatch(ct):
        return ct
    return "image/jpeg"


# -----------------------------------------------------------------------------
# Routing with robust rawPath parsing fallback
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Route:
    name: str
    allowed_methods: Tuple[str, ...]
    job_id_from_path: Optional[str]


def classify_route(path: str) -> Route:
    """
    Classify routes by path shape (independent of method), enabling:
    - fallback parsing even when pathParameters are missing
    - clean 405 responses when path exists but method is wrong
    """
    # Match /.../report/{jobId}/photo-url
    match = re.search(r"/report/([^/]+)/photo-url$", path)
    if match:
        return Route(
            name="photo_presign",
            allowed_methods=("POST", "OPTIONS"),
            job_id_from_path=match.group(1),
        )

    # Match /.../report/{jobId}
    match = re.search(r"/report/([^/]+)$", path)
    if match:
        return Route(
            name="report_get",
            allowed_methods=("GET", "OPTIONS"),
            job_id_from_path=match.group(1),
        )

    # Match /.../report
    if re.search(r"/report$", path):
        return Route(
            name="report_upsert",
            allowed_methods=("POST", "OPTIONS"),
            job_id_from_path=None,
        )

    return Route(name="not_found", allowed_methods=(), job_id_from_path=None)


def resolve_job_id(
    path_params: Mapping[str, str],
    job_id_from_path: Optional[str],
) -> str:
    """
    Resolve jobId from either pathParameters or rawPath parsing.
    If both are present and disagree, treat as invalid input.
    """
    job_id_param = path_params.get("jobId")
    if job_id_param and job_id_from_path and job_id_param != job_id_from_path:
        raise ValueError("jobId mismatch between pathParameters and path")
    return job_id_param or job_id_from_path or ""


# -----------------------------------------------------------------------------
# Presign helpers
# -----------------------------------------------------------------------------

def build_object_key(job_id: str, filename: str) -> str:
    return f"reports/{job_id}/{uuid.uuid4()}-{filename}"


def sse_put_params_and_headers(
    config: AppConfig,
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """
    Optional SSE parameters/headers for presigning when bucket policies
    require encryption headers.
    """
    params: Dict[str, Any] = {}
    headers: Dict[str, str] = {}

    if config.presign_sse == "none":
        return params, headers

    params["ServerSideEncryption"] = config.presign_sse
    headers["x-amz-server-side-encryption"] = config.presign_sse

    if config.presign_sse == "aws:kms" and config.presign_sse_kms_key_id:
        params["SSEKMSKeyId"] = config.presign_sse_kms_key_id
        headers[
            "x-amz-server-side-encryption-aws-kms-key-id"
        ] = config.presign_sse_kms_key_id

    return params, headers


def presign_put(
    s3_client: Any,
    config: AppConfig,
    bucket: str,
    key: str,
    content_type: str,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"Bucket": bucket, "Key": key}
    required_headers: Dict[str, str] = {}

    # Avoid signing Content-Type by default (config.sign_content_type=false).
    if config.sign_content_type:
        params["ContentType"] = content_type
        required_headers["Content-Type"] = content_type

    sse_params, sse_headers = sse_put_params_and_headers(config)
    params.update(sse_params)
    required_headers.update(sse_headers)

    url = s3_client.generate_presigned_url(
        ClientMethod="put_object",
        Params=params,
        ExpiresIn=config.presign_expires,
        HttpMethod="PUT",
    )
    return {
        "method": "PUT",
        "url": url,
        "requiredHeaders": required_headers,
    }


def presign_post(
    s3_client: Any,
    config: AppConfig,
    bucket: str,
    key: str,
    content_type: str,
) -> Dict[str, Any]:
    fields: Dict[str, str] = {}
    conditions = []

    # Only constrain Content-Type when explicitly configured.
    if config.sign_content_type:
        fields["Content-Type"] = content_type
        conditions.append({"Content-Type": content_type})

    if config.presign_sse != "none":
        fields["x-amz-server-side-encryption"] = config.presign_sse
        conditions.append({"x-amz-server-side-encryption": config.presign_sse})

        if config.presign_sse == "aws:kms" and config.presign_sse_kms_key_id:
            fields[
                "x-amz-server-side-encryption-aws-kms-key-id"
            ] = config.presign_sse_kms_key_id
            conditions.append(
                {
                    "x-amz-server-side-encryption-aws-kms-key-id":
                        config.presign_sse_kms_key_id
                }
            )

    # Note: You can add a size limit by adding a condition like:
    # ["content-length-range", 1, 10485760]
    # This is intentionally not enabled by default (unspecified).
    post = s3_client.generate_presigned_post(
        Bucket=bucket,
        Key=key,
        Fields=fields or None,
        Conditions=conditions or None,
        ExpiresIn=config.presign_expires,
    )
    return {
        "method": "POST",
        "url": post.get("url"),
        "fields": post.get("fields", {}),
    }


# -----------------------------------------------------------------------------
# Business handlers
# -----------------------------------------------------------------------------

def handle_photo_presign(
    event: Mapping[str, Any],
    config: AppConfig,
    s3_client: Any,
    request_id: Optional[str],
    job_id_raw: str,
) -> Dict[str, Any]:
    if not config.photos_bucket:
        return error(
            config,
            500,
            "config_error",
            "PHOTOS_BUCKET is not configured",
            request_id=request_id,
        )

    try:
        job_id = validate_job_id(job_id_raw)
    except ValueError as exc:
        return error(
            config,
            400,
            "invalid_job_id",
            str(exc),
            request_id=request_id,
        )

    try:
        body = parse_json_body(event)
    except ValueError as exc:
        return error(
            config,
            400,
            "invalid_request",
            str(exc),
            request_id=request_id,
        )

    filename = sanitize_filename(str(body.get("filename") or "photo.jpg"))
    content_type = sanitize_content_type(str(body.get("contentType") or ""))

    object_key = build_object_key(job_id, filename)

    log_json(
        logging.DEBUG,
        "presign_request",
        requestId=request_id,
        mode=config.presign_mode,
        bucket=config.photos_bucket,
        key=object_key,
        expiresIn=config.presign_expires,
        signContentType=config.sign_content_type,
        sse=None if config.presign_sse == "none" else config.presign_sse,
    )

    try:
        if config.presign_mode == "post":
            upload = presign_post(
                s3_client,
                config,
                config.photos_bucket,
                object_key,
                content_type,
            )
        else:
            upload = presign_put(
                s3_client,
                config,
                config.photos_bucket,
                object_key,
                content_type,
            )
    except (ClientError, BotoCoreError) as exc:
        log_json(
            logging.ERROR,
            "presign_failed",
            requestId=request_id,
            errorType=type(exc).__name__,
            error=str(exc),
        )
        return error(
            config,
            500,
            "presign_failed",
            "Failed to generate presigned upload parameters",
            request_id=request_id,
        )

    return response(
        config,
        200,
        {
            "jobId": job_id,
            "objectKey": object_key,
            "photoLocation": f"s3://{config.photos_bucket}/{object_key}",
            "presign": {
                "mode": config.presign_mode,
                "expiresIn": config.presign_expires,
                "signContentType": config.sign_content_type,
                "contentType": content_type,
                "upload": upload,
            },
        },
    )


def handle_report_upsert(
    event: Mapping[str, Any],
    config: AppConfig,
    table: Any,
    request_id: Optional[str],
) -> Dict[str, Any]:
    try:
        body = parse_json_body(event)
    except ValueError as exc:
        return error(
            config,
            400,
            "invalid_request",
            str(exc),
            request_id=request_id,
        )

    try:
        job_id = validate_job_id(str(body.get("jobId") or ""))
    except ValueError as exc:
        return error(
            config,
            400,
            "invalid_job_id",
            str(exc),
            request_id=request_id,
        )

    body["jobId"] = job_id

    try:
        table.put_item(Item=body)
    except (ClientError, BotoCoreError) as exc:
        log_json(
            logging.ERROR,
            "dynamodb_put_failed",
            requestId=request_id,
            table=config.table_name,
            jobId=job_id,
            errorType=type(exc).__name__,
            error=str(exc),
        )
        return error(
            config,
            500,
            "dynamodb_put_failed",
            "Failed to save report",
            request_id=request_id,
        )

    return response(config, 200, {"message": "Report saved", "jobId": job_id})


def handle_report_get(
    config: AppConfig,
    table: Any,
    request_id: Optional[str],
    job_id_raw: str,
) -> Dict[str, Any]:
    try:
        job_id = validate_job_id(job_id_raw)
    except ValueError as exc:
        return error(
            config,
            400,
            "invalid_job_id",
            str(exc),
            request_id=request_id,
        )

    try:
        resp = table.get_item(Key={"jobId": job_id})
    except (ClientError, BotoCoreError) as exc:
        log_json(
            logging.ERROR,
            "dynamodb_get_failed",
            requestId=request_id,
            table=config.table_name,
            jobId=job_id,
            errorType=type(exc).__name__,
            error=str(exc),
        )
        return error(
            config,
            500,
            "dynamodb_get_failed",
            "Failed to fetch report",
            request_id=request_id,
        )

    item = resp.get("Item")
    if not item:
        return error(
            config,
            404,
            "not_found",
            "Report not found",
            request_id=request_id,
            details={"jobId": job_id},
        )

    return response(config, 200, item)


# -----------------------------------------------------------------------------
# Dispatch / unit-test hook
# -----------------------------------------------------------------------------

def _dispatch(
    event: Mapping[str, Any],
    context: Any,
    config: AppConfig,
    s3_client: Any,
    table: Any,
) -> Dict[str, Any]:
    request_id = get_request_id(event, context)
    method = get_method(event)
    path = get_path(event)
    path_params = get_path_params(event)

    route = classify_route(path)

    log_json(
        logging.INFO,
        "request",
        requestId=request_id,
        method=method,
        path=path,
        route=route.name,
        version=event.get("version") or "1.0 (assumed if absent)",
    )

    if method == "OPTIONS":
        return response(config, 200, {"message": "ok"})

    if route.name == "not_found":
        return error(
            config,
            404,
            "not_found",
            "Not found",
            request_id=request_id,
            details={"method": method, "path": path},
        )

    if method not in route.allowed_methods:
        return error(
            config,
            405,
            "method_not_allowed",
            "Method not allowed",
            request_id=request_id,
            details={"allowed": route.allowed_methods, "method": method},
        )

    if route.name == "photo_presign":
        try:
            job_id_raw = resolve_job_id(path_params, route.job_id_from_path)
        except ValueError as exc:
            return error(
                config,
                400,
                "invalid_request",
                str(exc),
                request_id=request_id,
            )
        return handle_photo_presign(
            event,
            config,
            s3_client,
            request_id,
            job_id_raw,
        )

    if route.name == "report_upsert":
        return handle_report_upsert(event, config, table, request_id)

    if route.name == "report_get":
        try:
            job_id_raw = resolve_job_id(path_params, route.job_id_from_path)
        except ValueError as exc:
            return error(
                config,
                400,
                "invalid_request",
                str(exc),
                request_id=request_id,
            )
        return handle_report_get(config, table, request_id, job_id_raw)

    return error(
        config,
        500,
        "internal_error",
        "Unhandled route",
        request_id=request_id,
        details={"route": route.name},
    )


# -----------------------------------------------------------------------------
# Lambda entrypoint (lazy init for production stability)
# -----------------------------------------------------------------------------

_APP: Optional[Tuple[AppConfig, Any, Any]] = None
_APP_INIT_ERROR: Optional[str] = None


def get_app() -> Tuple[AppConfig, Any, Any]:
    global _APP, _APP_INIT_ERROR  # noqa: PLW0603

    if _APP is not None:
        return _APP

    try:
        config = load_config()
        s3_client = create_s3_client(config)
        table = create_dynamodb_table(config)
        _APP = (config, s3_client, table)
        return _APP
    except Exception as exc:  # noqa: BLE001
        _APP_INIT_ERROR = str(exc)
        LOGGER.exception("app_init_failed")
        raise


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    try:
        config, s3_client, table = get_app()
    except Exception:
        # Minimal fallback response if initialization fails before config loads.
        headers = {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "OPTIONS,GET,POST",
        }
        body: Dict[str, Any] = {
            "error": {
                "code": "init_failed",
                "message": "Initialization failed",
            }
        }
        if _APP_INIT_ERROR:
            body["error"]["details"] = {"reason": _APP_INIT_ERROR}
        return {"statusCode": 500, "headers": headers, "body": json.dumps(body)}

    return _dispatch(event, context, config, s3_client, table)
