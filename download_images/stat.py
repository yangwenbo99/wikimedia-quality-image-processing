#!/usr/bin/python3
"""Query statistics from query-detailed-meta.py output JSON files."""

import argparse
import json
from pathlib import Path
from collections import Counter
from datetime import datetime
from typing import List, Dict, Any, Optional

import matplotlib.pyplot as plt
import matplotlib.dates as mdates


def load_items(dir_path: Path) -> List[Dict[str, Any]]:
    """Load all page items from JSON files in dir_path."""
    items: List[Dict[str, Any]] = []
    for p in sorted(dir_path.glob("*.json")):
        with open(p, "r") as f:
            data = json.load(f)
            print(f"Loaded {type(data).__name__} from {p}")
        if isinstance(data, list):
            items.extend(data)
        else:
            items.append(data)
    return items


def license_value(ii: Dict[str, Any]) -> Optional[str]:
    """Get License value from one imageinfo entry (supports extmetadata)."""
    ext = ii.get("extmetadata") or {}
    lic = ext.get("License") or ii.get("License")
    if lic and isinstance(lic, dict):
        return lic.get("value")
    return None


def datetime_value(ii: Dict[str, Any]) -> Optional[str]:
    """Get DateTime value from one imageinfo entry."""
    ext = ii.get("extmetadata") or {}
    dt = ext.get("DateTime") or ii.get("DateTime")
    if dt and isinstance(dt, dict):
        return dt.get("value")
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Statistics from query-detailed-meta.py output"
    )
    parser.add_argument(
        "dir",
        type=Path,
        help="Directory containing query result JSON files",
    )
    parser.add_argument(
        "-O",
        "--output-dir",
        metavar="DIR",
        type=Path,
        default=None,
        help="Write all outputs into DIR (histogram, license counts, plot)",
    )
    config = parser.parse_args()
    dir_path = config.dir
    if not dir_path.is_dir():
        raise SystemExit(f"Not a directory: {dir_path}")

    out_dir = config.output_dir
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    items = load_items(dir_path)

    # 1. Histogram of len(item['imageinfo'])
    lengths = [len(item.get("imageinfo")) for item in items if item is not None and "imageinfo" in item]
    hist = Counter(lengths)
    print("Histogram of imageinfo length (length -> count):")
    hist_lines = ["Histogram of imageinfo length (length -> count):"]
    for k in sorted(hist.keys()):
        line = f"  {k}: {hist[k]}"
        print(line)
        hist_lines.append(line)
    if out_dir is not None:
        (out_dir / "histogram_imageinfo_len.txt").write_text(
            "\n".join(hist_lines) + "\n"
        )
    print()

    # 2. Count of each used licence, ascending order by count
    license_counts: Counter[str] = Counter()
    for item in items:
        for ii in item.get("imageinfo") or []:
            val = license_value(ii)
            if val is not None:
                license_counts[val] += 1
    print("License counts (ascending by count):")
    lic_lines = ["License counts (ascending by count):"]
    for lic, cnt in sorted(license_counts.items(), key=lambda x: x[1]):
        line = f"  {lic}: {cnt}"
        print(line)
        lic_lines.append(line)
    if out_dir is not None:
        (out_dir / "license_counts.txt").write_text("\n".join(lic_lines) + "\n")
    print()
    count_no_attribute = 0
    count_attribute_only = 0
    count_all = 0
    for lic in license_counts.keys():
        if lic in ['pd', 'cc0']:
            count_no_attribute += license_counts[lic]
        if lic in ['pd', 'cc0'] or (lic.startswith('cc-by') and not lic.startswith('cc-by-sa')):
            count_attribute_only += license_counts[lic]
        count_all += license_counts[lic]
    print(f"Images without explicit attribution requirements: {count_no_attribute} ({count_no_attribute/count_all:.2%})")
    print(f"Images with only attribution requirement: {count_attribute_only} ({count_attribute_only/count_all:.2%})")
    print(f"Images with any share-alike requirement: {count_all - count_attribute_only} ({(count_all - count_attribute_only)/count_all:.2%})")
    print()

    # 3. Parse and plot DateTime
    datetimes: List[datetime] = []
    for item in items:
        for ii in item.get("imageinfo") or []:
            val = datetime_value(ii)
            if not val:
                continue
            val = val.strip()
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y:%m:%d %H:%M:%S",
                "%Y-%m-%d",
            ):
                try:
                    datetimes.append(datetime.strptime(val, fmt))
                    break
                except ValueError:
                    continue

    if not datetimes:
        print("No DateTime values found, skipping plot.")
        return

    fig, ax = plt.subplots()
    ax.hist(datetimes, bins=min(80, max(20, len(datetimes) // 50)), edgecolor="k")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    plt.xticks(rotation=45)
    plt.xlabel("DateTime")
    plt.ylabel("Count")
    plt.title("Distribution of image DateTime (metadata)")
    plt.tight_layout()
    plot_path = out_dir / "datetime_hist.png" if out_dir else None
    if plot_path is not None:
        plt.savefig(plot_path, dpi=150)
        print(f"Plot saved to {plot_path}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
