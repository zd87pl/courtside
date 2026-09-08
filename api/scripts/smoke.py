#!/usr/bin/env python3
"""Exercise a deployment with the operator's own key and test video."""
import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--analyze", action="store_true", help="Run one clip using the paid model provider")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()
    key = os.environ.get("COURTSIDE_API_KEY")
    if not key:
        parser.error("Set COURTSIDE_API_KEY in your environment")
    if not args.video.is_file():
        parser.error("video must be an existing file")
    base = args.base_url.rstrip("/")

    def request(method, path, body=None):
        req = urllib.request.Request(base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "X-Courtside-User-Id": os.environ.get("COURTSIDE_USER_ID", "smoke-test-user")})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)

    assert request("GET", "/readyz")["ok"], "deployment is not ready"
    size = args.video.stat().st_size
    up = request("POST", "/v1/uploads", {"filename": args.video.name,
                 "content_type": "video/mp4", "size_bytes": size, "multipart": True})
    jid = up["job_id"]
    print(f"Reserved job {jid}", flush=True)
    parts = []
    with args.video.open("rb") as source:
        for part in up["parts"]:
            req = urllib.request.Request(part["url"], method="PUT", data=source.read(up["part_size"]))
            # Object URLs carry their own credentials; never forward the API key.
            with urllib.request.urlopen(req, timeout=300) as resp:
                parts.append({"part_number": part["part_number"], "etag": resp.headers["ETag"]})
    request("POST", f"/v1/uploads/{jid}/complete", {"parts": parts})
    if not args.analyze:
        request("POST", f"/v1/jobs/{jid}/cancel")
        print("Upload/complete/cancel passed; no model calls. Worker maintenance and bucket lifecycle handle source cleanup.")
        return
    request("POST", f"/v1/jobs/{jid}/start", {"options": {"max_clips": 1, "moments": 0, "pose": False}})
    deadline = time.monotonic() + args.timeout
    previous = None
    while time.monotonic() < deadline:
        job = request("GET", f"/v1/jobs/{jid}")
        state = (job["status"], job["phase"], job["progress"])
        if state != previous:
            print(*state, flush=True)
            previous = state
        if job["status"] == "succeeded":
            report = request("GET", f"/v1/jobs/{jid}/report")
            for name in ("report_html_url", "session_json_url", "report_markdown_url"):
                with urllib.request.urlopen(report["report"][name], timeout=60) as resp:
                    assert resp.read(1), f"empty artifact: {name}"
            print("Analysis and artifact downloads passed.")
            return
        if job["status"] in ("failed", "cancelled", "expired"):
            raise RuntimeError(f"Job {jid}: {job['status']} ({job.get('error_code')})")
        time.sleep(5)
    raise RuntimeError(f"Smoke timeout; job {jid} may still be running. Poll or cancel it explicitly.")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"HTTP {exc.code}; check deployment logs. URLs and credentials withheld.") from None
