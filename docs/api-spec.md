# ServiceTech API Spec (v0.1)

Base URL (prod):

```text
https://ulmjbpdrdc.execute-api.us-west-1.amazonaws.com

**POST /report**

Create or update a service report.

Method: POST

Path: /report

Headers:

Content-Type: application/json

Body example:

{
  "jobId": "JOB-001",
  "technician": "Maggie",
  "customer": "Acme Corp",
  "notes": "Replaced faulty capacitor.",
  "date": "2026-02-25T18:30:00Z",
  "status": "Completed"
}

Success (200)

{
  "message": "Report saved",
  "jobId": "JOB-001"
}

**GET /report/{jobId}**

Retrieve a report by job ID.

Method: GET

Path: /report/{jobId}

Success (200)

{
  "jobId": "JOB-001",
  "technician": "...",
  "customer": "...",
  "notes": "...",
  "date": "...",
  "status": "..."
}

Not found

{
  "error": "Report not found"
}