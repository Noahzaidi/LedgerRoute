"""Package the maintained LedgerRoute source without local settings or Git metadata.

Run from any working directory: python scripts/package_source.py
The archive is written to output/ledgerroute-source.zip inside the repository.
"""

import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    ".dockerignore",
    ".env.example",
    ".gitattributes",
    ".gitignore",
    "CONTRIBUTING.md",
    "Dockerfile",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "pyproject.toml",
)
SOURCE_PATTERNS = {
    ".github/workflows": "*.yml",
    "app": "*.py",
    "docs": "*.md",
    "scripts": "*.py",
    "tests": "*.py",
}
DATA_FILES = (
    "README.md",
    "historical_resolutions.csv",
    "historical_resolutions.json",
    "new_post_ocr_inputs.csv",
    "new_post_ocr_inputs.json",
    "reference_data.json",
)


def main() -> None:
    paths = [PROJECT_ROOT / filename for filename in ROOT_FILES]
    paths.extend(PROJECT_ROOT / "data" / filename for filename in DATA_FILES)
    for directory, pattern in SOURCE_PATTERNS.items():
        paths.extend((PROJECT_ROOT / directory).rglob(pattern))
    archive = PROJECT_ROOT / "output" / "ledgerroute-source.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(paths):
            relative = path.relative_to(PROJECT_ROOT)
            # Do not follow symlinks that could include files outside the source tree.
            if path.is_symlink() or not path.resolve().is_relative_to(PROJECT_ROOT):
                raise ValueError(f"Source path must stay inside the repository: {relative}")
            bundle.write(path, (Path("ledgerroute") / relative).as_posix())
    print(f"Created {archive.name} with {len(paths)} source files in {archive.parent.name}/")


if __name__ == "__main__":
    main()
