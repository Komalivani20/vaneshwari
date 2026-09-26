import sys
from google.cloud import firestore

# Hardcode project ID as requested to prevent Agent Platform fallback issues
PROJECT_ID = "qwiklabs-gcp-02-5c5faf8ba4fb"
COLLECTION_NAME = "incidents"

db = firestore.Client(project=PROJECT_ID)

SEED_INCIDENTS = [
    {
        "incident_id": "INC-1001",
        "server_id": "web-prod-01",
        "severity": "CRITICAL",
        "status": "OPEN",
        "summary": "Out of memory error in web application worker pool",
        "details": "High memory consumption leading to OOMKilled state on container web-prod-01",
        "created_at": "2026-09-26T10:15:00Z",
    },
    {
        "incident_id": "INC-1002",
        "server_id": "api-gateway-02",
        "severity": "WARNING",
        "status": "OPEN",
        "summary": "Latency spike above threshold (>1200ms)",
        "details": "Database connection pool exhaustion causing slow response times",
        "created_at": "2026-09-26T10:45:00Z",
    },
    {
        "incident_id": "INC-1000",
        "server_id": "db-primary-01",
        "severity": "CRITICAL",
        "status": "RESOLVED",
        "summary": "Disk capacity reached 95%",
        "details": "Old log rotation cleared 40GB space. Status marked resolved.",
        "created_at": "2026-09-25T18:30:00Z",
    },
]


def seed():
    print(f"Seeding Firestore collection '{COLLECTION_NAME}' in project '{PROJECT_ID}'...")
    collection_ref = db.collection(COLLECTION_NAME)
    for incident in SEED_INCIDENTS:
        doc_ref = collection_ref.document(incident["incident_id"])
        doc_ref.set(incident)
        print(f"  ✓ Added document {incident['incident_id']} ({incident['server_id']})")
    print("Firestore seeding complete!")


if __name__ == "__main__":
    seed()
