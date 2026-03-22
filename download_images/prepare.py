#!/usr/bin/python3

import argparse
import csv
import html
import re
from pathlib import Path
import PIL
from PIL import Image
import json
import os
import shutil
import sys
import time
import multiprocessing

PIL.Image.MAX_IMAGE_PIXELS = 933120000
ALLOWED_SUFFIX = ['.jpeg', '.jpg', '.tiff', '.tif']
QUALITY = 97


def _ext_text_value(ii: dict, key: str) -> str:
    """Extract a plain-text value from extmetadata."""
    ext = ii.get("extmetadata") or {}
    val = ext.get(key)
    if isinstance(val, dict):
        val = val.get("value")
    if not isinstance(val, str):
        return ""
    s = html.unescape(val)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _load_filter_json(
    filter_json_path: str,
) -> list[dict]:
    """Load meta-filter JSON and return the item list.

    Each item is expected to carry a ``_disk_stem`` field
    (added by ``meta-filter.py``).
    """
    with open(filter_json_path, 'r') as f:
        return json.load(f)


class ImageProcessor: 

    def process_one_image(self, src_file_path): 
        fname = src_file_path.stem + '.jpg'
        dest_file_path = self.dest_path / fname
        if dest_file_path.is_file() and os.path.getsize(dest_file_path) > 1000: 
            return 
        img = Image.open(src_file_path)
        ratio = img.height / img.width
        if ratio < config.ratio_min or ratio > config.ratio_max: 
            return 
        if (
            img.height < config.min_height
            or img.width < config.min_width
        ):
            return
        below_target = (
            img.height < config.height
            or img.width < config.width
        )
        if below_target:
            suffix = src_file_path.suffix.lower()
            if suffix in ('.jpg', '.jpeg'):
                shutil.copy2(src_file_path, dest_file_path)
            else:
                img.save(
                    dest_file_path, "JPEG",
                    quality=QUALITY,
                )
            print('Done (original size)', src_file_path)
            return
        if ratio >= self.desired_aspect_ratio: 
            # Image taller than desired, shrink width to desired
            new_height, new_width = round(ratio * config.width), config.width
        else: 
            new_height, new_width = config.height, round(config.height / ratio) 
        new_img = img.resize( (new_width, new_height), Image.LANCZOS)
        new_img.save(dest_file_path, "JPEG", quality=QUALITY)
        
        print('Done', src_file_path)

    def catched_process_one_image(self, src_file_path): 
        try:
            self.process_one_image(src_file_path)
        except (OSError, UserWarning) as e: 
            print(f"Warning: IO error when processing {src_file_path}", file=sys.stderr)
            print(e.strerror, file=sys.stderr)


    def main(self, config):
        self.dest_path = Path(config.dest)
        self.desired_aspect_ratio = config.height / config.width
        self.dest_path.mkdir(parents=True, exist_ok=True)

        src_dir = Path(config.source)
        csv_path = self.dest_path / 'file_list.csv'

        if config.filter_json:
            items = _load_filter_json(config.filter_json)
            stem_to_item = {}
            for item in items:
                ds = item.get('_disk_stem')
                if ds:
                    stem_to_item[ds] = item
            print(
                f'{len(stem_to_item)} stems loaded from '
                f'meta-filter JSON'
            )

            src_file_paths = []
            csv_rows: list[dict[str, str]] = []
            for stem in sorted(stem_to_item):
                for suffix in ALLOWED_SUFFIX:
                    p = src_dir / (stem + suffix)
                    if p.is_file():
                        src_file_paths.append(p)
                        item = stem_to_item[stem]
                        ii_list = item.get(
                            "imageinfo"
                        ) or [{}]
                        ii = ii_list[0]
                        csv_rows.append({
                            'file': p.name,
                            'object_name':
                                _ext_text_value(
                                    ii, "ObjectName",
                                ),
                            'description':
                                _ext_text_value(
                                    ii,
                                    "ImageDescription",
                                ),
                        })
                        break
            print(
                f'{len(src_file_paths)} files found '
                f'on disk after meta-filter'
            )

            with open(csv_path, 'w', newline='') as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=[
                        'file',
                        'object_name',
                        'description',
                    ],
                )
                w.writeheader()
                w.writerows(csv_rows)
        else:
            src_file_paths = [
                x
                for x in sorted(src_dir.iterdir())
                if x.suffix.lower() in ALLOWED_SUFFIX
            ]
            print(
                f'{len(src_file_paths)} files with '
                f'allowed extension found'
            )

            with open(csv_path, 'w', newline='') as f:
                w = csv.DictWriter(
                    f, fieldnames=['file'],
                )
                w.writeheader()
                for p in src_file_paths:
                    w.writerow({'file': p.name})

        print(f'File list written to {csv_path}')

        pool = multiprocessing.Pool(
            processes=config.process,
        )
        pool.map(
            self.catched_process_one_image,
            src_file_paths,
        )


ip = ImageProcessor()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Download wikimedia contents from TOC')
    parser.add_argument(
        '-s', '--source',
        help='source dir', required=True,
    )
    parser.add_argument(
        '-d', '--dest',
        help='destination dir', required=True,
    )
    parser.add_argument(
        '-f', '--filter-json', dest='filter_json',
        help='meta-filter JSON to restrict source files',
    )
    parser.add_argument('--ratio-min', dest='ratio_min', help='minimum h/w ratio', type=float, default=0.38)
    parser.add_argument('--ratio-max', dest='ratio_max', help='maximum h/w ratio', type=float, default=2.62)
    parser.add_argument('--height', help='minimum height', type=int, default=1080)
    parser.add_argument('--width', help='minimum width', type=int, default=1080)
    parser.add_argument(
        '--min-height', dest='min_height',
        help='absolute minimum height; images below this '
             'are discarded, images between this and '
             '--height are kept at original size '
             '(default: same as --height)',
        type=int, default=None,
    )
    parser.add_argument(
        '--min-width', dest='min_width',
        help='absolute minimum width; images below this '
             'are discarded, images between this and '
             '--width are kept at original size '
             '(default: same as --width)',
        type=int, default=None,
    )
    parser.add_argument(
        '-p', '--process',
        help='number of processes',
        type=int, default=24,
    )
    config = parser.parse_args()
    if config.min_height is None:
        config.min_height = config.height
    if config.min_width is None:
        config.min_width = config.width
    ip.main(config)


