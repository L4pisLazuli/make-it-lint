"""bad/ and good/ layout: one pair, or several named variants."""

from __future__ import annotations

from pathlib import Path

DIFF_DIR_NAMES = frozenset({"base", "head"})


def is_multi_side(side_dir: Path) -> bool:
    """True when a side uses <side>/real/ (plus optional named variants)."""
    if not side_dir.is_dir():
        return False
    return (side_dir / "real").is_dir()


def is_multi_fixture(bad: Path, good: Path) -> bool:
    return is_multi_side(bad) or is_multi_side(good)


def stray_multi_files(side_dir: Path) -> list[Path]:
    """Files sitting directly under a multi-variant side directory instead of inside a variant."""
    if not is_multi_side(side_dir):
        return []
    return [
        child
        for child in sorted(side_dir.iterdir())
        if not child.name.startswith(".") and not child.is_dir()
    ]


def list_bad_variants(bad: Path, *, multi: bool | None = None) -> list[tuple[str, Path]]:
    """(label, directory) for each bad tree. Single-pair yields one entry named bad."""
    use_multi = is_multi_side(bad) if multi is None else multi
    if not use_multi:
        return [("bad", bad)]
    variants: list[tuple[str, Path]] = []
    if (bad / "real").is_dir():
        variants.append(("bad/real", bad / "real"))
    if bad.is_dir():
        for child in sorted(bad.iterdir()):
            if (
                child.is_dir()
                and not child.name.startswith(".")
                and child.name != "real"
                and child.name not in DIFF_DIR_NAMES
            ):
                variants.append((f"bad/{child.name}", child))
    return variants


def list_good_variants(good: Path, *, multi: bool) -> list[tuple[str, Path]]:
    """(label, directory) for each good tree. Multi uses every named subdirectory."""
    if not multi:
        return [("good", good)]
    variants: list[tuple[str, Path]] = []
    if (good / "real").is_dir():
        variants.append(("good/real", good / "real"))
    if good.is_dir():
        for child in sorted(good.iterdir()):
            if (
                child.is_dir()
                and not child.name.startswith(".")
                and child.name != "real"
                and child.name not in DIFF_DIR_NAMES
            ):
                variants.append((f"good/{child.name}", child))
    return variants


def all_fixture_dirs(bad: Path, good: Path) -> list[Path]:
    multi = is_multi_fixture(bad, good)
    dirs = [path for _, path in list_bad_variants(bad, multi=multi)]
    dirs.extend(path for _, path in list_good_variants(good, multi=multi))
    return dirs
