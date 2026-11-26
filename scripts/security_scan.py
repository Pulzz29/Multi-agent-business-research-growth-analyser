"""
Dependency security scan: pip-audit (Python, backend/requirements.txt as
installed in .venv) and npm audit (frontend production dependencies).

Exit code 1 when either tool reports a known vulnerability; 2 when a scanner
could not run. Results: docs/validation_evidence/security_scan_<UTC>.json.

Usage: .venv\\Scripts\\python.exe scripts\\security_scan.py
(needs: pip install -r backend/requirements-dev.txt; network access to the
vulnerability databases, no credentials are sent)
"""

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd, cwd):
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return result, None


def main():
    report = {"started_at_utc": datetime.now(timezone.utc).isoformat()}
    status = 0

    result, error = run([sys.executable, "-m", "pip_audit", "--format", "json", "--progress-spinner", "off"], ROOT)
    if error or result is None or result.returncode not in (0, 1):
        report["python"] = {"ran": False, "error": error or (result.stderr[-500:] if result else "")}
        status = max(status, 2)
    else:
        data = json.loads(result.stdout or "{}")
        vulnerable = [{"name": d["name"], "version": d["version"],
                       "ids": [v["id"] for v in d.get("vulns", [])],
                       "fix_versions": sorted({f for v in d.get("vulns", []) for f in v.get("fix_versions", [])})}
                      for d in data.get("dependencies", []) if d.get("vulns")]
        report["python"] = {"ran": True, "packages": len(data.get("dependencies", [])), "vulnerable": vulnerable}
        status = max(status, 1 if vulnerable else 0)

    npm = shutil.which("npm") or shutil.which("npm.cmd")
    result, error = run([npm, "audit", "--omit=dev", "--json"], ROOT / "frontend") if npm else (None, "npm not found")
    if error or result is None:
        report["frontend"] = {"ran": False, "error": error}
        status = max(status, 2)
    else:
        data = json.loads(result.stdout or "{}")
        counts = (data.get("metadata") or {}).get("vulnerabilities") or {}
        report["frontend"] = {"ran": True, "vulnerabilities": counts,
                              "packages": sorted((data.get("vulnerabilities") or {}).keys())}
        if sum(v for k, v in counts.items() if k != "total") or counts.get("total"):
            status = max(status, 1)

    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    out = ROOT / "docs" / "validation_evidence" / f"security_scan_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k in ("python", "frontend")}, indent=2))
    print("Evidence:", out.relative_to(ROOT))
    return status


if __name__ == "__main__":
    sys.exit(main())
