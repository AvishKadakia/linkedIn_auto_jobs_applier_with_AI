#!/usr/bin/env python3
import os
import argparse
from pathlib import Path
from typing import Optional

import pathspec


def load_ignore_spec(root: Path, ignore_filename: str) -> Optional[pathspec.PathSpec]:
    """
    Load a .gitignore-style file from `root/ignore_filename` and
    return a PathSpec that can test paths against it.
    """
    ignore_path = root / ignore_filename
    if not ignore_path.is_file():
        return None

    lines = ignore_path.read_text(encoding="utf-8").splitlines()
    spec = pathspec.GitIgnoreSpec.from_lines(lines)
    return spec


def is_ignored(path: Path, root: Path, spec: Optional[pathspec.PathSpec]) -> bool:
    """Return True if `path` should be ignored according to `spec`."""
    if spec is None:
        return False

    rel = path.relative_to(root).as_posix()
    return spec.match_file(rel)


def is_text_file(path: Path, blocksize: int = 1024) -> bool:
    """
    Heuristic check: consider a file 'text' if the first block has no NUL bytes.
    This helps avoid dumping binaries into the LLM context.
    """
    try:
        with path.open("rb") as f:
            chunk = f.read(blocksize)
        if b"\0" in chunk:
            return False
        return True
    except Exception:
        return False


def is_dockerfile(path: Path) -> bool:
    """
    True if this is a Dockerfile.
    """
    return path.name == "Dockerfile"


def is_docker_compose_file(path: Path) -> bool:
    """
    True if this looks like a docker-compose file.
    Common names: docker-compose.yml / docker-compose.yaml
    """
    name = path.name.lower()
    return name in {"docker-compose.yml", "docker-compose.yaml"}


def should_include_file(path: Path) -> bool:
    """
    Only include:
      - .py files
      - Dockerfile
      - docker-compose.yml / docker-compose.yaml
      - .toml files
    """
    if is_dockerfile(path):
        return True
    if is_docker_compose_file(path):
        return True
    if path.suffix == ".py":
        return True
    if path.suffix == ".toml":
        return True
    return False


def dump_project(root_dir: Path, output_file: Path, ignore_filename: str) -> None:
    root_dir = root_dir.resolve()
    output_file = output_file.resolve()

    spec = load_ignore_spec(root_dir, ignore_filename)

    with output_file.open("w", encoding="utf-8") as out_f:
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirpath = Path(dirpath)

            # Filter out ignored directories so os.walk doesn't descend into them
            dirnames[:] = [
                d
                for d in dirnames
                if not is_ignored(dirpath / d, root_dir, spec)
            ]

            for filename in filenames:
                file_path = dirpath / filename

                # Don't include the output file itself
                if file_path == output_file:
                    continue

                # Skip ignored files
                if is_ignored(file_path, root_dir, spec):
                    continue

                # Only include selected file types
                if not should_include_file(file_path):
                    continue

                # Optionally skip binary-ish files even if they match extension
                if not is_text_file(file_path):
                    continue

                rel_path = file_path.relative_to(root_dir).as_posix()

                # Heading with NO space: "#path/to/file.py"
                out_f.write(f"#{rel_path}\n\n")

                try:
                    with file_path.open(
                        "r", encoding="utf-8", errors="replace"
                    ) as in_f:
                        for line in in_f:
                            out_f.write(line)
                except Exception as e:
                    out_f.write(f"# [Error reading file {rel_path}: {e}]\n")

                out_f.write("\n\n")  # blank line between files


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Dump selected project files into a single text file, "
            "respecting a .gitignore-style ignore file."
        )
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Root folder to walk (default: current directory).",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="project_dump.txt",
        help="Output text file path (default: project_dump.txt).",
    )
    parser.add_argument(
        "-i",
        "--ignore-file",
        default=".gitignore",
        help="Ignore file name (default: .gitignore).",
    )

    args = parser.parse_args()

    root_dir = Path(args.root)
    output_file = Path(args.output)
    dump_project(root_dir, output_file, args.ignore_file)


if __name__ == "__main__":
    main()
