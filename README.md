Overview

ServiceReportsApp is an API + future UI for field service techs to create and retrieve job reports. Backend is AWS API Gateway → Lambda → DynamoDB.

Architecture

    -API Gateway HTTP API (ulmjbpdrdc)

    -Lambda: ServiceReportHandler

    -DynamoDB table: ServiceReports (on-demand)

Endpoints

    -Document the ones that work today:

### POST /report
Creates or updates a service report.

Request body (JSON):
{
  "jobId": "JOB-001",
  "technician": "Maggie",
  "notes": "Replaced capacitor",
  "date": "2026-02-25"
}

Response: 200
{
  "message": "Report saved",
  "jobId": "JOB-001"
}

### GET /report/{jobId}
Returns the stored report or 404-equivalent JSON if not found.

Environment / config

    -Region: us-west-1

    -Env vars:

        -TABLE_NAME=ServiceReports

    -Any IAM notes, e.g.:

        -Lambda role has least-privilege dynamodb:PutItem/GetItem on ServiceReports.

Deployment

    -Push code to GitHub, copy lambda_function.py into Lambda UI or deploy via zip.

    -API Gateway is manually configured to hit ServiceReportHandler.