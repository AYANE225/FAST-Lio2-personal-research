"""Check baseline, engineering and career artifact sizes and SHA256 without ROS."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    count = 0
    for name in ('data/checksums.json', 'data/engineering_checksums.json', 'data/career_checksums.json'):
        entries = json.loads((ROOT / name).read_text())
        for relative, expected in entries.items():
            relative = Path(relative)
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError(f'Invalid artifact path: {relative}')
            path = ROOT / relative
            if not path.is_file() or path.stat().st_size != expected['size_bytes']:
                raise ValueError(f'Missing or wrong-size artifact: {relative}')
            with path.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != expected['sha256']:
                raise ValueError(f'SHA256 mismatch: {relative}')
            count += 1
    print(f'Verified {count} artifact sizes and SHA256 hashes.')


if __name__ == '__main__':
    main()
