#!/usr/bin/env bash
# Reset a fixture directory to its checked-in contents.
#
# Usage: ./fixtures/reset_fixture.sh <fixture-name>
# Example: ./fixtures/reset_fixture.sh sample_service
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <fixture-name>" >&2
  exit 1
fi

fixture_name="$1"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fixture_path="fixtures/${fixture_name}"

if [[ ! -d "${script_dir}/${fixture_name}" ]]; then
  echo "no such fixture: ${fixture_path}" >&2
  exit 1
fi

repo_root="$(cd "${script_dir}/.." && pwd)"
cd "${repo_root}"

git checkout -- "${fixture_path}"
git clean -fdx -- "${fixture_path}"

echo "reset ${fixture_path} to its checked-in contents"
