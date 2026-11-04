"""Publish only the approved tracker rows to the protected portal. Never sends mail."""
import json
import os
import sys
import time
from pathlib import Path
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

ORIGIN = "https://triada-team-access.mayakorol87.chatgpt.site"
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the ingestion credential to another location.

def publish(html, records, token, *, opener=None, sleep=None):
    if not token:
        raise ValueError("TRIADA_PORTAL_INGEST_TOKEN is missing; no data was sent")
    if not isinstance(records,list) or not records or "<!doctype html" not in html.lower():
        raise ValueError("An approved nonempty tracker build is required")
    if html.count('<tr data-ts=') != len(records):
        raise ValueError("Tracker HTML and approved records have different counts")
    opener, sleep = opener or build_opener(NoRedirect()).open, sleep or time.sleep
    request = Request(ORIGIN + "/api/ingest", data=json.dumps({"html":html,"records":records},ensure_ascii=False).encode(),headers={"Content-Type":"application/json","Accept":"application/json","User-Agent":"TRIADA-Media-Tracker/1.0 (+https://github.com/Maiiatriada/triada-media-intelligence-staging)","Origin":ORIGIN,"Authorization":"Bearer " + token},method="POST")
    for attempt in range(3):
        try:
            with opener(request,timeout=90) as response:
                result=json.load(response)
            if result.get("updated") is not True or result.get("total") != len(records):
                raise ValueError("Portal did not acknowledge the complete approved dataset")
            return result
        except (HTTPError,URLError,TimeoutError) as error:
            if attempt == 2 or isinstance(error,HTTPError) and error.code not in {429,500,502,503,504}:
                raise RuntimeError("Portal sync failed; credentials omitted; next run can safely retry") from None
            sleep((2,5)[attempt])

def main():
    if len(sys.argv)!=3: raise SystemExit("Usage: publish_tracker.py approved.html approved-records.json")
    try:
        result=publish(Path(sys.argv[1]).read_text(encoding="utf-8"),json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")),os.environ.get("TRIADA_PORTAL_INGEST_TOKEN",""))
        print(json.dumps(result))
    except (ValueError,RuntimeError) as error: raise SystemExit(str(error))
if __name__=="__main__": main()
