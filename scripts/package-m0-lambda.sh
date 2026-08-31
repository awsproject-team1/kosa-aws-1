#!/usr/bin/env bash
# Build a deterministic M0 Lambda ZIP with every import and synthetic fixture required at runtime.
set -euo pipefail

output_path=${1:?usage: scripts/package-m0-lambda.sh <output-zip-path>}
repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
output_directory=$(dirname "${output_path}")
output_name=$(basename "${output_path}")

mkdir -p "${output_directory}"
output_path=$(cd "${output_directory}" && pwd)/${output_name}
rm -f "${output_path}"

if command -v python3 >/dev/null 2>&1; then
  python_command=python3
elif command -v python >/dev/null 2>&1; then
  python_command=python
else
  echo "Python 3 is required to package the M0 Lambda artifact" >&2
  exit 1
fi

(
  cd "${repository_root}"
  "${python_command}" - "${output_path}" <<'PY'
from pathlib import Path
import sys
from zipfile import ZIP_STORED, ZipFile, ZipInfo

output_path = Path(sys.argv[1])
repository_root = Path.cwd()
source_roots = (Path("agent"), Path("apps"), Path("packages"), Path("fixtures"))
required_paths = {
    "agent/runtime/__init__.py",
    "apps/backend/api/runtime.py",
    "apps/backend/assessment/runtime.py",
    "packages/contracts/__init__.py",
    "fixtures/m0/policy_profile.json",
    "fixtures/m0/assessment_model_profile.json",
    "fixtures/m0/s3_resource_snapshot.json",
}
source_files = sorted(
    path
    for source_root in source_roots
    for path in source_root.rglob("*")
    if path.is_file()
    and "__pycache__" not in path.parts
    and path.suffix not in {".pyc", ".pyo", ".pyd"}
)

with ZipFile(output_path, "w", compression=ZIP_STORED) as archive:
    for source_path in source_files:
        archive_path = source_path.as_posix()
        entry = ZipInfo(archive_path, date_time=(1980, 1, 1, 0, 0, 0))
        entry.compress_type = ZIP_STORED
        entry.create_system = 3
        entry.external_attr = 0o100644 << 16
        archive.writestr(entry, (repository_root / source_path).read_bytes())

with ZipFile(output_path) as archive:
    missing_paths = sorted(required_paths.difference(archive.namelist()))
if missing_paths:
    raise RuntimeError(
        "Lambda package is missing required entries: " + ", ".join(missing_paths)
    )
PY
)
