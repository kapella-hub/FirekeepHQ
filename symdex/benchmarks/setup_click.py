"""Clone and index the Click repository for benchmarking."""

import subprocess
import sys
from pathlib import Path

# Add FirekeepSymdex src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

REPO_URL = "https://github.com/pallets/click.git"
REPOS_DIR = Path(__file__).parent / "repos"
CLICK_DIR = REPOS_DIR / "click"
CLICK_SRC = CLICK_DIR / "src" / "click"


def clone_click() -> Path:
    """Clone Click repo with --depth 1. Skips if already cloned."""
    if CLICK_DIR.exists():
        print(f"Click repo already exists at {CLICK_DIR}")
        return CLICK_DIR

    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Cloning Click into {CLICK_DIR} ...")
    subprocess.run(
        ["git", "clone", "--depth", "1", REPO_URL, str(CLICK_DIR)],
        check=True,
    )
    print("Clone complete.")
    return CLICK_DIR


def index_click() -> dict:
    """Index Click's src/click directory with FirekeepSymdex."""
    from firekeep_symdex.tools.index_folder import index_folder

    if not CLICK_SRC.is_dir():
        raise FileNotFoundError(
            f"Click source not found at {CLICK_SRC}. Run clone_click() first."
        )

    print(f"Indexing {CLICK_SRC} ...")
    result = index_folder(
        path=str(CLICK_SRC),
        use_ai_summaries=False,
        storage_path=str(REPOS_DIR / ".click-index"),
    )
    return result


def main() -> None:
    clone_click()
    result = index_click()

    # Print summary
    files = result.get("file_count", 0)
    symbols = result.get("symbol_count", 0)
    print(f"\nIndex complete: {files} files, {symbols} symbols")
    print(f"Storage: {REPOS_DIR / '.click-index'}")


if __name__ == "__main__":
    main()
