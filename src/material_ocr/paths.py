"""Package resources must work independently of the current directory."""
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent


def rebar_config(name: str) -> Path:
    return PACKAGE_ROOT / "materials" / "rebar" / "config" / name


def review_template() -> Path:
    return PACKAGE_ROOT / "review" / "templates" / "same_page.html"
