"""Account-wide admission limits, shared by every API process."""
import time


def rate(c, account_id, action: str, limit: int):
    from .jobs import AdmissionError
    row = c.execute("""INSERT INTO rate_limits(scope, bucket, used) VALUES (%s, %s, 1)
        ON CONFLICT(scope, bucket) DO UPDATE SET used = rate_limits.used + 1
        RETURNING used""", (f"{account_id}:{action}", int(time.time()) // 3600)).fetchone()
    if row["used"] > limit:
        raise AdmissionError(429, f"Hourly {action} limit reached; retry in the next UTC hour.")
