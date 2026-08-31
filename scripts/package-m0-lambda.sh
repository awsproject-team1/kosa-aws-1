#!/usr/bin/env bash
# Build the M0 Lambda ZIP with every import and synthetic fixture required at runtime.
set -euo pipefail

output_path=${1:?usage: scripts/package-m0-lambda.sh <output-zip-path>}
repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
output_directory=$(dirname "${output_path}")
output_name=$(basename "${output_path}")

mkdir -p "${output_directory}"
output_path=$(cd "${output_directory}" && pwd)/${output_name}
rm -f "${output_path}"

(
  cd "${repository_root}"
  zip --quiet --recurse-paths "${output_path}" apps packages fixtures \
    --exclude '*/__pycache__/*' '*.py[cod]'
)

for required_path in \
  apps/backend/api/runtime.py \
  apps/backend/assessment/runtime.py \
  packages/contracts/__init__.py \
  fixtures/m0/policy_profile.json \
  fixtures/m0/assessment_model_profile.json \
  fixtures/m0/s3_resource_snapshot.json; do
  unzip -Z1 "${output_path}" | grep --fixed-strings --quiet -- "${required_path}"
done
