#!/usr/bin/env bash
# Build a deterministic AWS Lambda Layer ZIP containing LangGraph and its runtime
# dependencies for the Parent Orchestrator and workflow subgraphs (ADR-0012).
#
# The function ZIP (scripts/package-m0-lambda.sh) stays framework-free and holds only
# first-party source; third-party wheels live in this Layer instead. Keeping them apart
# preserves the deterministic source build and keeps the plan_hash approval boundary
# (Terraform show-json projection only, ADR-0019 §1) independent of the wheel set.
#
# Layer layout follows the AWS convention: everything under a top-level `python/`
# directory is importable by the function. We pin to the exact runtime the Lambda uses
# (python 3.12, manylinux2014 x86_64) so the wheels match the execution environment.
set -euo pipefail

output_path=${1:?usage: scripts/build-langgraph-layer.sh <output-zip-path>}
repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
requirements_file="${repository_root}/apps/backend/requirements.txt"

python_version=3.12
platform=manylinux2014_x86_64

output_directory=$(dirname "${output_path}")
output_name=$(basename "${output_path}")
mkdir -p "${output_directory}"
output_path=$(cd "${output_directory}" && pwd)/${output_name}
rm -f "${output_path}"

build_root=$(mktemp -d)
trap 'rm -rf "${build_root}"' EXIT
target_dir="${build_root}/python"
mkdir -p "${target_dir}"

if command -v python3 >/dev/null 2>&1; then
  python_command=python3
else
  python_command=python
fi

# Install only binary wheels for the Lambda runtime target. --only-binary=:all: fails
# closed if any dependency would need a source build, which would not match the Lambda
# environment and would break reproducibility.
"${python_command}" -m pip install \
  --requirement "${requirements_file}" \
  --target "${target_dir}" \
  --platform "${platform}" \
  --python-version "${python_version}" \
  --implementation cp \
  --only-binary=:all: \
  --upgrade \
  --no-compile

# Strip non-deterministic and unnecessary artifacts: bytecode caches and dist-info
# RECORD timestamps do not affect import and would perturb the digest.
find "${target_dir}" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "${target_dir}" -type f -name '*.pyc' -delete

# Deterministic ZIP: fixed mtime, sorted entries, stored order stable across machines.
(
  cd "${build_root}"
  "${python_command}" - "${output_path}" "${target_dir}" <<'PY'
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

output_path = Path(sys.argv[1])
build_root = Path.cwd()
files = sorted(
    path for path in build_root.rglob("*") if path.is_file()
)
with ZipFile(output_path, "w", compression=ZIP_DEFLATED) as archive:
    for source_path in files:
        archive_name = source_path.relative_to(build_root).as_posix()
        entry = ZipInfo(archive_name, date_time=(1980, 1, 1, 0, 0, 0))
        entry.compress_type = ZIP_DEFLATED
        entry.create_system = 3
        entry.external_attr = 0o100644 << 16
        archive.writestr(entry, source_path.read_bytes())
PY
)

echo "LangGraph Layer written to ${output_path}"
"${python_command}" - "${output_path}" <<'PY'
import sys
from zipfile import ZipFile

with ZipFile(sys.argv[1]) as archive:
    names = archive.namelist()
required = ("python/langgraph/", "python/langchain_core/", "python/pydantic/")
missing = [prefix for prefix in required if not any(n.startswith(prefix) for n in names)]
if missing:
    raise SystemExit("Layer is missing required packages: " + ", ".join(missing))
print(f"Layer contains {len(names)} entries; required packages present.")
PY
