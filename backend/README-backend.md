# Backend – ServiceTech AutoReporter

This directory contains the backend Lambda code for the service reports app.
This is JL adding a useless comment to this file

## Lambda function

- Name in AWS: `ServiceReportHandler`
- Runtime: Python
- Entry file: `lambda_function.py`
- Handler: `lambda_function.lambda_handler`
- Triggered by: API Gateway HTTP API

## DynamoDB

- Table: `ServiceReports`
- Region: `us-west-1`
- Primary key: `jobId` (string)
- Billing mode: On-demand (PAY_PER_REQUEST)

## Endpoint behavior

**POST /report**

- Reads JSON from `event["body"]`
- Requires at least a `jobId` field
- Stores the payload as an item in the `ServiceReports` table
- Returns a 200 with `{ "message": "Report saved", "jobId": "<id>" }`

**GET /report/{jobId}**

- Reads `jobId` from `event["pathParameters"]["jobId"]`
- Fetches the item from DynamoDB
- Returns the item JSON if found
- Returns an error JSON if not found